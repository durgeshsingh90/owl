# OWL feature test and customer journey plan

- Work-prompt order: 002
- Version: 1.0
- Status: Required validation contract
- Product source of truth: `work_prompts/001_OWL_MASTER_REQUIREMENTS.md`
- Applies to: local OWL development, phase acceptance, release testing, and defect retesting
- Last consolidated: 25 August 2026

## 1. Purpose and authority

The master requirements define what OWL must do. This file defines how a person or Codex proves that the features work from the customer's point of view.

Use this document to:

- validate each implementation phase through the visible interface;
- verify the underlying data, security, accessibility, and recovery behavior;
- give every important workflow a stable customer-journey and test ID;
- collect redacted evidence consistently;
- produce actionable defects with expected and actual results;
- make a release decision against explicit exit criteria.

If this file conflicts with the master requirements, the master requirements take precedence and both files must be corrected before relying on the affected test.

Passing a unit or integration test is not a substitute for completing the mapped visible customer journey. A browser journey is not a substitute for the lower-level security, data-integrity, migration, or performance checks. Both views are required in proportion to the feature's risk.

## 2. Safety, privacy, and execution boundaries

### 2.1 Default test boundary

All public CI and normal automated tests use synthetic data only. They must not contact a real Confluence site, Bitbucket instance, private Git host, or other internal service.

Use:

- a temporary disposable database;
- a temporary OWL data root;
- a mocked Confluence adapter;
- an isolated fake `SecretStore`;
- temporary local Git repositories;
- small generated PDFs containing invented text;
- fake notes, tags, people, paths, searches, and timestamps;
- invalid example hosts beneath `.invalid` when a non-resolving hostname is required;
- fake tokens such as `owl-test-pat-never-valid`.

Automated tests must never inspect, change, delete, or prompt for the user's real macOS Keychain items. A test is defective if it requires a real PAT.

### 2.2 Optional live-integration boundary

Live Confluence or Git/Bitbucket checks are allowed only when the user explicitly requests them, the appropriate opt-in flag is set, and credentials have been supplied locally through the approved secure mechanism.

Live checks must:

- use read-only permissions;
- operate only on targets the user placed in scope;
- avoid source writes and destructive operations;
- redact internal origins, repository URLs, titles, people, document text, paths, and tokens from reports;
- keep screenshots and traces local and ignored by Git;
- make no assumption that live data is disposable.

### 2.3 Stop and continue rules

Stop the test run immediately and preserve evidence if any of these occurs:

- a PAT, password, private key, cookie, authorization header, or credential-bearing URL appears in output or stored application data;
- OWL attempts an unapproved external request;
- OWL writes to Confluence or changes the source Git repository;
- a path escapes the configured temporary/runtime root;
- canonical OWL data is corrupted or unexpectedly lost;
- a destructive action affects a target that was not explicitly confirmed;
- the test would require inspecting or changing the user's real credential store.

Continue past ordinary non-blocking defects so related behavior can be assessed. Record each failure, avoid repeating a harmful action, and finish with a triage summary.

### 2.4 Forbidden evidence

Never place the following in Git, a public defect, a test name, a screenshot, a trace, or copied console output:

- real PATs or token fragments;
- real internal URLs, repository locations, filenames, or paths;
- private PDF content or extracted text;
- personal notes, searches, authors, or bookmark exports;
- real database, index, clone, log, backup, or credential-store content.

## 3. Result, priority, and severity vocabulary

### 3.1 Test results

| Result | Meaning |
|---|---|
| PASS | Every required checkpoint produced the expected result and evidence. |
| FAIL | At least one checkpoint produced an incorrect result. |
| BLOCKED | A stated prerequisite outside the test failed or was unavailable. |
| NOT RUN | The test has not been executed for this build. |
| NOT APPLICABLE | The test does not apply to the selected platform/configuration, with a recorded reason. |

Do not use **PASS WITH ISSUES**. Mark the test PASS only if its required checkpoints pass; record unrelated observations separately.

### 3.2 Priorities

| Priority | Meaning |
|---|---|
| P0 | First use, core workflow, security boundary, canonical data integrity, recovery, or release smoke. |
| P1 | Major daily workflow or significant productivity behavior. |
| P2 | Less common boundary, usability refinement, or non-blocking polish. |

### 3.3 Defect severity

| Severity | Meaning |
|---|---|
| Critical | Secret exposure, data loss, unapproved destructive/source write, arbitrary file access, or the application is unusable. |
| High | A P0/P1 customer goal cannot be completed and no practical safe workaround exists. |
| Medium | A major behavior is wrong, but a safe practical workaround exists. |
| Low | Minor usability, wording, visual, or low-impact edge defect. |

Priority describes the importance of a test. Severity describes the impact of an observed defect. They are related but not interchangeable.

## 4. Required test layers

Each feature is validated at the layers relevant to its risks:

1. **Unit:** parsing, normalization, state transitions, ordering, security decisions, and pure business rules.
2. **Integration:** database constraints, fake credential storage, mocked Confluence, temporary Git repositories, PDF extraction, FTS, jobs, backup, and migrations.
3. **Browser/customer journey:** visible actions and results through the rendered product.
4. **Accessibility:** keyboard, focus, accessible names, status announcements, semantics, contrast, zoom, and non-color state.
5. **Security:** secret non-disclosure, CSRF, SSRF, redirect/origin restrictions, command/path safety, output escaping, and public-repository safety.
6. **Resilience:** partial failure, cancellation, interruption, retry, restart, and last-good-state preservation.
7. **Performance:** representative volume, measured latency, progress visibility, concurrency, and bounded resources.
8. **Manual visual review:** principal screens and their empty, loading, progress, success, partial, error, and stale states.

### 4.1 Navigation and application-shell regression contract

Every supported page must preserve this product hierarchy and route contract:

- the top-level navigation contains **Home**, **Bookmark Manager**, and **Bitbucket Search**;
- the OWL logo opens **Home**. Home is a concise overview, while Bookmark Manager and
  Bitbucket Search remain the two working tools;
- Bookmark Manager has an app-specific left sidebar containing **All bookmarks**,
  **Favorites**, **Pinned**, **Recently viewed**, **Frequently viewed**,
  **Never viewed**, **Import JSON**, **Export JSON**, and **Confluence settings**. The
  central hierarchy remains the visual focus, and the persistent **Confluence settings**
  toolbar gear remains available;
- Bitbucket Search has an app-specific left sidebar containing **Search PDFs**,
  **Repositories**, and **Index & refresh status**, plus relevant configuration or
  diagnostics;
- **Global Search** is absent from top-level navigation until it is implemented. Its
  reserved route remains valid and, when the feature is implemented, it is exposed as a
  shared home/utility capability rather than a third top-level application;
- **System Status** is reachable as a footer/sidebar utility and is not a top-level
  application.

Navigation regression tests exercise the existing routes without changing their purpose:

| Purpose | Required route |
|---|---|
| Home | `/` |
| Reserved/future Global Search | `/search/` |
| Bookmark Manager | `/bookmarks/` |
| Bookmark Manager settings fallback | `/bookmarks/settings/` |
| Bitbucket Search | `/pdfs/` |
| Repositories | `/pdfs/repositories/` |
| Index & refresh status | `/pdfs/status/` |
| System status | `/system-status/` |

Home, the two working tools, and the current app sidebar use separately labelled navigation landmarks.
The current application and sidebar destination are identified by text and appropriate
`aria-current` state, not color alone. A keyboard user can reach and operate every
destination with visible focus. At 200% zoom and supported narrow desktop widths, the
sidebar may become an accessible drawer only if all destinations and core actions remain
available, opening does not lose the current app context, Escape/close works, and focus
returns to the trigger.

## 5. Test environments and fixtures

### 5.1 Environment profiles

| Profile | Purpose | External access | Data |
|---|---|---|---|
| CI synthetic | Repeatable automated gate | None | Generated fixtures only |
| Local synthetic | Browser journeys and exploratory testing | Loopback fixture services only | Temporary generated data |
| Clean-machine smoke | Prove setup/run documentation | None by default | Generated fixtures only |
| Performance | Measure documented representative Mac | None required | Approved synthetic/representative local corpus |
| Live integration | Optional adapter confirmation | Explicitly approved targets only | Private local evidence, never committed |

### 5.2 Synthetic Confluence fixture

Provide deterministic mocked pages that include:

- a three-level hierarchy with two bookmarks sharing ancestors;
- modern URL, legacy URL, and raw ID inputs for the same Page ID;
- two different Page IDs with similar titles;
- a page that is later renamed;
- a page that is later moved to a different parent;
- a page whose version advances after it is opened;
- a never-opened page whose version advances;
- 401, 403, 404, 429, timeout, TLS/connection, malformed-response, and 5xx outcomes;
- recovery from each recoverable failure;
- characters requiring safe HTML escaping;
- enough generated records to exercise pagination, lazy tree rendering, and filtering.

Use an invented origin such as `https://confluence.example.invalid`. A successful browser test must use a mocked adapter or explicitly loopback fixture path rather than trying to resolve that hostname.

### 5.3 Synthetic credential fixture

Provide an injectable fake `SecretStore` with deterministic modes:

- store/read/delete success;
- unavailable backend;
- access denied;
- write failure;
- delete failure;
- interrupted/failed replacement;
- restart persistence within the isolated test profile.

The fake PAT value is unique per test run so automated checks can search for accidental disclosure. It must be harmless and clearly invalid outside the mock.

### 5.4 Synthetic Git and PDF fixture

Temporary Git repositories must support commits for:

- initial clone;
- unchanged repeat sync;
- PDF added;
- PDF content changed;
- PDF removed;
- confident rename;
- ambiguous remove/add;
- removed PDF reappearing;
- dirty working tree;
- non-fast-forward update;
- one repository failing while another succeeds.

Generated PDF fixtures must include:

- readable one-page and multi-page documents;
- `Private Link` as one exact phrase;
- terms distributed across repository, path, filename, notes, and separate PDF pages;
- same-page multi-chip terms for ranking comparison;
- image-only/no-text PDF;
- corrupt input;
- encrypted PDF;
- Git LFS pointer instead of a PDF object;
- HTML-significant text for escaping checks;
- enough repeated synthetic documents for pagination and performance measurement.

### 5.5 Run record

Every formal run records:

~~~text
Run ID:
Build/commit:
Branch and dirty-state summary:
Date/time and timezone:
Tester:
Operating system:
Browser and viewport:
Python/Django versions:
Fixture version:
Environment profile:
Journeys/tests selected:
Start/end time:
PASS / FAIL / BLOCKED / NOT RUN counts:
Defect IDs:
Sanitized evidence location:
Cleanup result:
Release recommendation:
~~~

## 6. Customer journey execution format

Every customer journey below contains:

- a stable ID and customer goal;
- priority and mapped master acceptance scenarios;
- prerequisites and synthetic data;
- visible customer steps;
- expected checkpoints;
- failure/recovery coverage;
- evidence and cleanup requirements.

For browser execution, interact with the same visible controls a customer uses. Direct database or API setup may create synthetic preconditions, but it cannot replace the visible steps being tested.

## 7. Customer journeys

### CJ-001 — First launch and secure Confluence setup

- **Persona:** first-time local knowledge worker
- **Goal:** connect Bookmark Manager without editing a hidden file
- **Priority:** P0
- **Covers:** master acceptance 89–100; supports 4, 26, 83–86
- **Prerequisites:** clean temporary database, empty fake `SecretStore`, mocked Confluence connection outcomes

Steps and checkpoints:

1. Start OWL with no Confluence environment variables at **Home**.
   - Navigation offers **Home**, **Bookmark Manager**, and **Bitbucket Search**. Home is
     the concise overview, while Bookmark Manager and Bitbucket Search are the two working
     tools; the OWL logo identifies Home.
   - Open **Bookmark Manager** and confirm its app-specific sidebar is present without
     displacing the central tree-first workspace.
   - The page loads with local/offline functionality intact.
   - A first-use message explains that Confluence is not configured.
   - The top-right gear is visible and named **Confluence settings**.
2. Open the gear with a mouse, close it with Escape, and open it again with the keyboard.
   - The settings drawer/panel has a clear heading and logical focus.
   - Closing returns focus to the gear.
3. Enter the synthetic HTTPS base URL and unique fake PAT.
   - The PAT is masked by default, spellcheck is disabled, and the value is not placed in the URL.
   - Show/Hide affects only the value currently typed.
4. Select **Cancel** and reopen settings.
   - Nothing was saved and the PAT field is empty.
5. Enter the values again and select **Test Connection**.
   - One explicit read-only request occurs.
   - A successful sanitized result appears, but restart proves the values were not saved.
6. Repeat, test successfully, and select **Save**.
   - The state becomes **Connected**.
   - The base URL/auth mode and verification metadata are stored; the PAT exists only in the fake secure store.
7. Reopen settings and restart OWL.
   - The state remains configured/connected.
   - The saved PAT is never returned; the replacement field stays empty and says **Stored securely**.
8. Exercise invalid credential, access denied, unreachable, and server-failure mock outcomes.
   - Results are distinct, actionable, and contain no upstream body or secret.
9. Attempt a base-URL change without a new PAT, then simulate a failed replacement, then complete a valid replacement.
   - A new PAT is required for a new canonical origin.
   - The old credential is never sent to the new origin.
   - Failed replacement leaves the last working profile intact; successful replacement is atomic.
10. Remove the UI-managed PAT and confirm the action.
    - Secure deletion occurs before local connected metadata is cleared.
    - Local bookmarks remain available; network-dependent actions explain what is missing.
11. Start a separate profile with a complete environment-managed configuration.
    - The UI says **Managed externally**, returns no environment values, and disables replace/remove.
12. Start with an incomplete environment profile and with an unavailable secure store.
    - Each condition gives a safe action-oriented error and no plaintext fallback.

Required evidence:

- screenshots of Not configured, Connected, one failure state, and Managed externally, with no entered PAT visible;
- sanitized network request count and result category;
- assertions that the unique fake PAT is absent from response bodies, rendered HTML, JavaScript state, cookies, session, browser storage, URLs, redirects, database rows, logs, exports, backups, traces, screenshots, process arguments, and tracked files;
- credential-store operation record containing only operation/result, never the value.

Cleanup: delete the isolated fake store and temporary database/data root. Do not touch the real credential store.

### CJ-002 — Save and reveal the first bookmark

- **Persona:** first-time Bookmark Manager user
- **Goal:** turn a known Confluence URL into an organized, findable bookmark
- **Priority:** P0
- **Covers:** 1, 3, 6, 7, 14–17

Steps and checkpoints:

1. Begin with a connected synthetic profile and an empty bookmark tree.
2. Paste a modern Confluence URL and save.
3. Confirm exactly one local bookmark is created and the status bar reports its permanent OWL number.
4. Confirm every real ancestor appears once, hierarchy-only ancestors have no OWL number, and the new bookmark is expanded, selected, scrolled into view, and briefly highlighted.
5. Inspect details and verify title, Page ID, space, people, version, dates, breadcrumb, URL, and initial usage state.
6. Search by title, URL, and Page ID and confirm each reveals the same tree node.

Failure branch: use invalid text and a disallowed host; each is rejected without a Confluence request or partial record.

Evidence: visible empty and populated states, database count/identity assertion, request count, status announcement, and keyboard reveal result.

### CJ-003 — Duplicate identity and similar titles

- **Persona:** returning Bookmark Manager user
- **Goal:** avoid duplicate bookmarks without blocking legitimately similar pages
- **Priority:** P1
- **Covers:** 2, 3, 5

Steps and checkpoints:

1. Save the raw Page ID, legacy URL, and a different modern URL for the Page ID used in CJ-002.
2. Confirm each selects the existing OWL number, creates no row, and makes no unnecessary Confluence request.
3. Save a second synthetic Page ID with a similar title.
4. Confirm it receives a different OWL number and a non-blocking similarity warning links both records.

### CJ-004 — Daily bookmark finding and organization

- **Persona:** frequent knowledge worker
- **Goal:** find, annotate, prioritize, and reuse bookmarks quickly
- **Priority:** P1
- **Covers:** 18–23, 32

Steps and checkpoints:

1. Expand/collapse branches and confirm expansion, selection, and scroll state survive minor updates and restart.
2. Search separately by title, ID, URL, space, person, breadcrumb, tag, and note.
3. Combine filters, inspect active chips/counts, clear them, and save/restore a view.
4. Add and edit a quick note and tags; verify escaped display and local search.
5. Favorite and pin independently; verify instant status feedback and persistence.
6. Copy Page ID and breadcrumb and verify exact values.
7. Open through OWL; confirm the validated new tab, atomic open count, first/last viewed times, and viewed version.
8. Exercise recent, frequent, never-viewed, favorite, pinned, and flat-sort views without changing the real tree.

Navigation and accessibility checkpoint: enter Bookmark Manager through the neutral
chooser, use its sidebar, then return through the OWL logo without losing the saved tree
state. Complete search, tree navigation, note, favorite, pin, copy, and open with keyboard
only and hear status changes through the live region.

### CJ-005 — Refresh, rename, move, and changed since viewed

- **Persona:** returning user maintaining current bookmarks
- **Goal:** update source metadata without losing personal organization
- **Priority:** P0
- **Covers:** 8–16, 22–25

Steps and checkpoints:

1. Prepare opened and never-opened bookmarks with notes, tags, favorite, pin, saved dates, and usage.
2. Advance both mocked Confluence versions and refresh one.
3. Confirm only the selected bookmark changes and **Changed since you last opened it** appears only for the previously opened record.
4. Rename and then move the source page; refresh and confirm the same Page ID/OWL number relocates with no stale duplicate.
5. Confirm every OWL-owned field is unchanged.
6. Multi-select a subset and use Refresh Selected; confirm untouched bookmarks make no request.
7. Use Refresh All and verify durable progress, counts, rate-limit handling, partial completion, and continued local search/navigation.

### CJ-006 — Confluence failure and recovery

- **Persona:** user recovering from connection or permission problems
- **Goal:** understand the problem and retain local knowledge until access returns
- **Priority:** P0
- **Covers:** 10–13, 26

Exercise confirmed 401, 403, 404, 429 exhaustion, timeout, malformed response, and 5xx on separate records.

Expected checkpoints:

- one global authentication failure stops duplicate requests and links to Confluence settings;
- 403 becomes ACCESS_DENIED, not NOT_FOUND;
- only confirmed 404 becomes NOT_FOUND;
- transient/invalid responses become REFRESH_ERROR;
- last-known metadata and all personal data remain available;
- batch work continues after item failures unless the failure is global authentication;
- a later successful refresh restores ACTIVE and preserves history.

### CJ-007 — Legacy import, backup export, and local delete

- **Persona:** user migrating an existing collection
- **Goal:** bring old bookmarks into OWL safely and retain a portable local backup
- **Priority:** P1
- **Covers:** 27–31, 81, 83

Steps and checkpoints:

1. Import a heterogeneous file with valid, duplicate, alternate-field, breadcrumb-only, and malformed records.
2. Confirm valid records continue, failures are record-level and sanitized, and deterministic OWL numbers are assigned.
3. Import the same file again and confirm idempotency.
4. Attempt to overwrite non-empty local notes/tags/favorite/pin/usage and confirm existing OWL-owned data wins.
5. Export, inspect the schema, and re-import into a clean database.
6. Confirm canonical values round-trip and no credential/secret-store reference is present.
7. Delete one OWL bookmark after confirmation and verify Confluence is untouched and shared tree nodes/siblings remain.

### CJ-008 — Add repositories and complete initial PDF indexing

- **Persona:** first-time Bitbucket Search user
- **Goal:** make a set of repository PDFs locally searchable
- **Priority:** P0
- **Covers:** 33, 34, 37–43, 53

Steps and checkpoints:

1. Open the neutral OWL home, choose **Bitbucket Search**, and select **Repositories** in
   its app-specific left sidebar.
   - The top-level chooser still contains exactly the two applications.
   - **Repositories** and **Index & refresh status** are Bitbucket sidebar destinations,
     not additional top-level applications.
2. Add multiple approved synthetic repository URLs in one action.
3. Include a duplicate, disallowed host, unsupported protocol, and credential-bearing URL; verify safe record-level results and redaction.
4. Start sync and confirm repositories clone once beneath the configured root.
5. Open **Index & refresh status** from the same sidebar and observe queued/running phases, totals, progress, failure details, and worker state without freezing search/navigation.
6. Confirm default-branch-only, case-insensitive PDF discovery and that all non-PDF files and escaping symlinks are excluded.
7. Confirm readable pages use one-based numbering and that no-text, corrupt, encrypted, parser-failure, and LFS-pointer inputs receive distinct safe states.
8. Verify the initial published index becomes searchable only after validation.

### CJ-009 — Find the right PDF with phrase-aware keyword chips

- **Persona:** knowledge worker searching a large technical corpus
- **Goal:** narrow results and understand exactly why each document matched
- **Priority:** P0
- **Covers:** 55–69

Steps and checkpoints:

1. Type `Private Link` and press Enter; confirm it becomes one phrase-aware chip.
2. Add more chips, reject a normalized duplicate, remove one, and clear all.
3. Run ALL mode where terms occur in repository, path, filename, notes, and separate PDF pages; confirm one document can satisfy the terms across enabled scopes.
4. Switch to ANY and confirm one matching chip is sufficient.
5. Enable each scope alone, then combine repository/path/status/star/collection/date/usage filters.
6. Compare exact filename, path-only, body-only, same-page, and scattered-page results; verify documented precedence.
7. Inspect per-chip field/page explanations and a safe limited snippet.
8. Open preview, move among strongest pages, and open the matched one-based page or see the documented fallback.
9. Confirm a successful OWL open updates usage while a failed open or Finder reveal does not.

### CJ-010 — Star, annotate, collect, and reuse PDFs

- **Persona:** frequent PDF user
- **Goal:** retain personal organization across repository changes
- **Priority:** P1
- **Covers:** 68–76

Steps and checkpoints:

1. Star/unstar from results and preview with immediate feedback.
2. Add one PDF to multiple collections and delete one collection; the PDF and notes remain.
3. Add document and page notes and find them through notes scope.
4. Copy selected paths and verify one path per line and clear scope wording.
5. Open through OWL and exercise Most Opened, Recently Opened, Never Opened, Starred, New, Updated, Removed, and Index Error views.
6. Save a search, change every control, rerun the saved search, and verify chips/mode/scopes/filters/sort restore exactly.
7. Restart OWL and confirm stars, collections, notes, history, saved searches, and usage persist.

### CJ-011 — Incremental synchronization and document lifecycle

- **Persona:** returning PDF user
- **Goal:** update the corpus quickly without losing metadata or a working search
- **Priority:** P0
- **Covers:** 35, 36, 44–54, 70–72

Steps and checkpoints:

1. Sync at the same commit; confirm fetch/fast-forward without reclone and zero extraction jobs.
2. Add, change, remove, rename, ambiguously remove/add, and reintroduce synthetic PDFs across commits.
3. Confirm only affected documents are processed.
4. Confirm a changed document atomically replaces old terms only after success.
5. Force changed extraction to fail; the last good index stays searchable and visibly stale.
6. Confirm a confident rename preserves star, collections, notes, and usage, while an ambiguous rename is not guessed.
7. Confirm removed documents are excluded by default, available through Removed, and preserve metadata; safe reappearance restores the record.
8. Make the working tree dirty and simulate non-fast-forward; confirm OWL does not reset/overwrite it and prior search remains usable.
9. Run a full rebuild and confirm staging validation plus atomic publication.

### CJ-012 — Interruption, retry, and partial failure

- **Persona:** user recovering after shutdown or one bad source
- **Goal:** resume work without duplicates or loss of the last good state
- **Priority:** P0
- **Covers:** 43, 47, 52–54, 80

Steps and checkpoints:

1. Interrupt repository sync, extraction, and rebuild at defined phases.
2. Restart OWL and inspect interrupted job records.
3. Retry failed/interrupted items and confirm no duplicate pages, documents, jobs, or index rows.
4. Fail one repository and one PDF while other work succeeds.
5. Confirm accurate partial-failure totals, retry actions, and sanitized diagnostics.
6. Search throughout and confirm the last published index remains usable.

### CJ-013 — Global search and dashboard orientation

- **Persona:** user who does not know which source contains the answer
- **Goal:** search bookmarks and PDFs from one place and understand current system state
- **Priority:** P1
- **Covers:** 78–80, 82

Steps and checkpoints:

1. Confirm the top-level navigation contains **Home**, **Bookmark Manager**, and
   **Bitbucket Search**. Global Search must not appear there while unimplemented.
2. Once Global Search is implemented, open it from the Home/shared utility entry
   at `/search/`; it must not become a third top-level application.
3. Create one query matching bookmark and PDF fixtures.
4. Confirm local grouped results, source counts, source labels, within-source ranking, and match explanations.
5. Open a bookmark result into its tree context and a PDF result into its preview/matched page.
6. Compare dashboard counts with canonical database/index totals.
7. Verify recent, changed, favorite/pinned/starred, broken, repository, and job sections.
8. Open **System Status** from the footer or an app sidebar, not the top-level app chooser,
   and confirm it reports database, FTS, worker, jobs, disk, and index health without
   secret values.

### CJ-014 — Backup, restore, migration, and credential re-entry

- **Persona:** user moving or recovering an OWL installation
- **Goal:** restore canonical personal data safely without copying credentials or rebuildable bulk data
- **Priority:** P0
- **Covers:** 30, 81–83, 88, 94, 100

Steps and checkpoints:

1. Create representative bookmarks, PDF metadata, personal organization, usage, saved searches, and non-secret source settings.
2. Produce a versioned backup with checksum.
3. Confirm PATs, secret-store references/presence flags, connected state, PDFs, clones, and derived FTS data are excluded.
4. Restore into a clean profile after confirmation and automatic safety backup.
5. Confirm immutable numbers and canonical personal data round-trip.
6. Confirm Confluence is unverified and requires PAT entry again.
7. Run fresh-install and upgrade-path migrations and a post-migration smoke test.

### CJ-015 — Keyboard, screen reader, zoom, and visual states

- **Persona:** keyboard or assistive-technology user
- **Goal:** complete core work without depending on a mouse, color, or a single viewport
- **Priority:** P0
- **Covers:** 16, 17, 21, 32, 64–67, 80, 89

Complete CJ-001, CJ-002, the key organization actions in CJ-004, and the core search/preview actions in CJ-009 without a mouse.

Verify:

- the neutral OWL home and logo are announced as a chooser/home, and the top-level
  application navigation exposes exactly **Bookmark Manager** and **Bitbucket Search**;
- each app sidebar has its own labelled navigation landmark, and both the active app and
  active sidebar destination expose text plus appropriate `aria-current` state;
- all app switching and sidebar destinations work by keyboard with visible focus, and
  returning through the OWL logo preserves the relevant app context;
- logical focus order and visible focus;
- accessible names and focusable tooltips for icon-only actions;
- correct panel/dialog focus containment and return;
- semantic tree/disclosure behavior and selected/expanded state;
- status-bar announcements without unwanted focus movement;
- exact date access without pointer hover;
- every status conveyed by text/icon, not color alone;
- 200% zoom and supported narrow desktop layout without hidden core actions; when a
  sidebar becomes a drawer, its trigger is named, Escape/close works, the current app
  remains clear, and focus returns to the trigger;
- reduced motion, contrast, and automated accessibility checks;
- empty, loading, active progress, success, no-result, partial-failure, unavailable, stale, and configuration-error screens.

### CJ-016 — Clean-machine release smoke

- **Persona:** new installer
- **Goal:** start OWL and complete its primary value journey from documentation alone
- **Priority:** P0
- **Covers:** 83–88 and all phase-level setup definitions of done

Steps and checkpoints:

1. Use a clean environment and follow only the root README/work-prompt instructions.
2. Install pinned dependencies, create the synthetic configuration, run migrations/checks, and start server plus worker.
3. Complete CJ-001, CJ-002, CJ-008, CJ-009, and an application restart.
4. Run the documented test/lint/format/secret/tracked-file checks.
5. Confirm OWL binds only to loopback and makes no unapproved external requests.
6. Confirm no runtime/private file is tracked and cleanup affects only the explicit temporary profile.

### CJ-017 — Browse bookmarks by month and year

- **Persona:** returning Bookmark Manager user
- **Goal:** find saved pages chronologically without losing their real Confluence location
- **Priority:** P1
- **Covers:** 101–102

Steps and checkpoints:

1. Freeze local time near a calendar-year boundary and create bookmarks across multiple current-year months and older years.
2. Confirm current-year groups use localized month names, older groups use one year heading, empty periods are absent, and both groups are newest first.
3. Inspect exact Added to OWL dates and accessible group headings with keyboard and screen-reader navigation.
4. Select entries from each group and confirm the canonical bookmark and details are revealed in the unchanged hierarchy.
5. Search and filter in the central workspace and confirm the timeline recalculates its groups, removes unrelated entries and empty headings, and still leaves the stored hierarchy unchanged.

### CJ-018 — Follow contributors and inspect their commits

- **Persona:** Bitbucket Search user tracing ownership and change history
- **Goal:** understand who authored, committed, opened, merged, or verifiably pushed PDF changes
- **Priority:** P1
- **Covers:** 103–108

Steps and checkpoints:

1. Build a synthetic repository whose configured branch is `master` and whose commit author, committer, PR author, merger, and verified push actor are deliberately different.
2. Sync recent history and confirm truthful role labels, distinct identities, hidden emails, and separate commit-history, push-evidence, and PR-history coverage states.
3. Scroll and keyboard-navigate PDF changes; confirm the right rail highlights the associated contributor using `aria-current` without moving focus.
4. Select each contributor and verify counts plus the complete available-history commit ledger, affected PDF links, repository, branch, dates, hashes, subjects, and role badges.
5. Include a direct commit and a commit with unavailable push/PR metadata; confirm OWL does not invent attribution.
6. Upgrade to Full History and confirm older Git activity appears without duplicates while missing push/PR evidence remains explicitly incomplete.
7. Close one unmerged PR and fulfill another; confirm only the fulfilled PR contributes to **Merged PRs**.
8. Confirm loopback OWL exposes no public webhook receiver and Cloud push actors remain unavailable unless the test uses an explicitly approved authenticated relay/import adapter.
9. Verify the contributor rail as a right-side sticky panel on desktop and a named accessible drawer/strip on a narrow screen.

## 8. Feature test matrix and traceability

The matrix is the minimum stable coverage map. Implementations may add narrower tests while retaining these IDs. **Automated target** indicates the expected repeatable layer; it does not remove the mapped browser journey.

### 8.1 Confluence configuration

| Test ID | Scenario | Priority | Layer/automated target | Master acceptance |
|---|---|---|---|---|
| CFG-001 | Gear visibility, label, keyboard operation, focus return | P0 | Browser + accessibility | 89 |
| CFG-002 | No-config first-use and offline-local behavior | P0 | Browser | 90 |
| CFG-003 | HTTPS origin validation and disallowed origin/redirect | P0 | Unit + integration | 4, 84, 91 |
| CFG-004 | Mask, show current input, cancel without save | P0 | Browser | 91, 96 |
| CFG-005 | Test Connection is explicit, read-only, bounded, and non-persistent | P0 | Integration + browser | 91, 93 |
| CFG-006 | Secure save/restart through fake SecretStore | P0 | Integration + browser | 91, 94 |
| CFG-007 | Stored PAT never redisplayed | P0 | Security + browser | 92, 100 |
| CFG-008 | 401, 403, connectivity, TLS, timeout, and 5xx result mapping | P0 | Integration + browser | 26, 93 |
| CFG-009 | Origin change requires new PAT; no cross-origin reuse | P0 | Unit + integration | 95 |
| CFG-010 | Atomic replace and old-profile preservation | P0 | Integration | 96 |
| CFG-011 | Confirmed removal and offline data preservation | P0 | Integration + browser | 97 |
| CFG-012 | Complete/incomplete environment profile and precedence | P0 | Unit + browser | 98 |
| CFG-013 | Secure-store unavailable/denied/failure; no plaintext fallback | P0 | Integration + browser | 99 |
| CFG-014 | PAT absence from every forbidden surface | P0 | Security gate | 83, 86, 100 |

### 8.2 Bookmark identity, tree, and organization

| Test ID | Scenario | Priority | Layer/automated target | Master acceptance |
|---|---|---|---|---|
| BMK-001 | Save valid modern URL and assign one immutable OWL number | P0 | Integration + browser | 1 |
| BMK-002 | Duplicate Page ID across URL forms avoids row/request | P0 | Unit + browser | 2, 3 |
| BMK-003 | Similar titles with distinct IDs remain distinct | P1 | Integration + browser | 5 |
| BMK-004 | Shared hierarchy reuse and hierarchy-only nodes | P0 | Integration + browser | 6, 7 |
| BMK-005 | Rename/move same identity without stale branch | P0 | Integration + browser | 8, 9 |
| BMK-006 | NEW/UPDATED/status priority and exact dates | P1 | Unit + browser | 14–16 |
| BMK-007 | Search reveals actual tree location | P0 | Integration + browser | 17 |
| BMK-008 | Combined filters preserve ancestor context | P1 | Integration + browser | 18 |
| BMK-009 | Flat sorting never mutates hierarchy | P1 | Integration + browser | 19 |
| BMK-010 | Notes are escaped/searchable and preserved | P1 | Integration + browser | 20 |
| BMK-011 | Favorite/pin independence and persistence | P1 | Integration + browser | 21 |
| BMK-012 | OWL open usage and viewed-version tracking | P0 | Integration + browser | 22, 23 |
| BMK-013 | Refresh Selected scope | P0 | Integration + browser | 24 |
| BMK-014 | Refresh All progress/rate limit/partial failure | P0 | Integration + browser | 25, 26 |
| BMK-015 | Import continuation, idempotency, ownership rules | P1 | Integration | 27–29 |
| BMK-016 | Export/re-import without credentials | P0 | Integration + security | 30 |
| BMK-017 | Confirmed local delete preserves source/shared tree | P0 | Integration + browser | 31 |
| BMK-018 | Core bookmark workflow is keyboard/screen-reader usable | P0 | Accessibility | 32 |
| BMK-019 | Added-to-OWL timeline groups current-year months and older years in local time | P1 | Unit + browser | 101 |
| BMK-020 | Timeline selection reveals the canonical tree item without mutating hierarchy | P1 | Integration + accessibility | 102 |

### 8.3 Confluence availability and recovery

| Test ID | Scenario | Priority | Layer/automated target | Master acceptance |
|---|---|---|---|---|
| REF-001 | Confirmed 404 becomes retained NOT_FOUND | P0 | Integration + browser | 10 |
| REF-002 | 403 becomes ACCESS_DENIED | P0 | Integration + browser | 11 |
| REF-003 | 429/timeout/malformed/5xx becomes retained REFRESH_ERROR | P0 | Integration + browser | 12 |
| REF-004 | Later success restores ACTIVE without data loss | P0 | Integration + browser | 13 |
| REF-005 | Global auth failure stops duplicate calls and guides recovery | P0 | Integration + browser | 26 |

### 8.4 Repository and index lifecycle

| Test ID | Scenario | Priority | Layer/automated target | Master acceptance |
|---|---|---|---|---|
| REP-001 | Multi-add, normalization, deduplication, concurrent sync | P0 | Integration + browser | 33 |
| REP-002 | Clone once and validate before READY | P0 | Integration | 34 |
| REP-003 | Existing clone fetch/fast-forward without reclone | P0 | Integration | 35 |
| REP-004 | Dirty/non-fast-forward preservation | P0 | Integration + resilience | 36 |
| REP-005 | Recent/full history explanation and confirmation | P1 | Integration + browser | 37, 38 |
| REP-006 | Default branch and PDF-only safe discovery | P0 | Integration | 39, 40 |
| REP-007 | LFS pointer never reaches parser | P0 | Integration + security | 41 |
| IDX-001 | Page extraction and one-based numbering | P0 | Integration | 42 |
| IDX-002 | No-text/corrupt/encrypted/parser failures are isolated | P0 | Integration | 43 |
| IDX-003 | Same-commit sync queues zero extraction | P0 | Integration | 44 |
| IDX-004 | Incremental add/change/remove only | P0 | Integration | 45 |
| IDX-005 | Successful atomic revision replacement | P0 | Integration | 46 |
| IDX-006 | Failed changed extraction retains visible stale index | P0 | Integration + browser | 47 |
| IDX-007 | Confident rename preserves metadata | P0 | Integration | 48 |
| IDX-008 | Ambiguous rename is not guessed | P0 | Integration | 49 |
| IDX-009 | Removed/reappearing document lifecycle | P0 | Integration + browser | 50, 51 |
| IDX-010 | Interrupted/retried jobs remain idempotent | P0 | Integration + resilience | 52 |
| IDX-011 | Search uses last published index during work | P0 | Integration + performance | 53 |
| IDX-012 | Full rebuild stages, validates, and switches atomically | P0 | Integration + resilience | 54 |
| GIT-001 | Default/master-branch commit author and committer remain truthful separate identities | P0 | Unit + integration | 103 |
| GIT-002 | Contributor identity normalization and current-result counts are accurate | P1 | Unit + integration | 104 |
| GIT-003 | Scroll/keyboard highlighting, `aria-current`, focus retention, and narrow layout work | P1 | Browser + accessibility | 105 |
| GIT-004 | Contributor ledger lists every available-history commit and affected PDF link | P1 | Integration + browser | 106 |
| GIT-005 | Per-source coverage and duplicate-free Full History commit expansion are explicit | P1 | Integration + browser | 107 |
| GIT-006 | PR author, fulfilled-state merger, non-merge closer, and authoritative push actor are separate; unavailable is never inferred | P0 | Adapter + security + browser | 108 |

### 8.5 PDF search and productivity

| Test ID | Scenario | Priority | Layer/automated target | Master acceptance |
|---|---|---|---|---|
| SEA-001 | Enter creates one phrase-aware chip | P0 | Unit + browser | 55 |
| SEA-002 | Chip normalization/deduplication/FTS escaping | P0 | Unit + security | 56 |
| SEA-003 | ALL across fields/pages and ANY semantics | P0 | Integration + browser | 57, 58 |
| SEA-004 | Each search scope independently works | P0 | Integration | 59 |
| SEA-005 | Filters combine correctly | P1 | Integration + browser | 60 |
| SEA-006 | Filename/path/body/same-page ranking precedence | P1 | Integration | 61, 62 |
| SEA-007 | Text relevance dominates capped personal boosts | P1 | Integration | 63 |
| SEA-008 | Match explanations and safe snippets | P0 | Integration + browser | 64, 65 |
| PDF-001 | Preview metadata and strongest pages | P1 | Browser | 66 |
| PDF-002 | Matched-page open/fallback correctness | P0 | Integration + browser | 67 |
| PDF-003 | Successful/failed/reveal usage counting | P0 | Integration | 68 |
| PDF-004 | Quick views return canonical documents | P1 | Integration + browser | 69 |
| PDF-005 | Star persistence across lifecycle | P1 | Integration | 70 |
| PDF-006 | Collections never own/delete documents or notes | P1 | Integration | 71 |
| PDF-007 | Document/page note search and preservation | P1 | Integration | 72 |
| PDF-008 | Deliberate search-history recording | P1 | Integration | 73 |
| PDF-009 | Saved search restores full query state | P1 | Integration + browser | 74 |
| PDF-010 | Explainable related documents | P2 | Unit + browser | 75 |
| PDF-011 | Copy-path scope and one-per-line output | P1 | Integration + browser | 76 |
| PDF-012 | Open All thresholds and arbitrary-path rejection | P0 | Integration + security | 77 |

### 8.6 Shared, operations, security, and performance

| Test ID | Scenario | Priority | Layer/automated target | Master acceptance |
|---|---|---|---|---|
| NAV-001 | Home plus two working tools, app-specific sidebars, utility placement, and stable routes | P0 | Integration + browser | 78–80, 82, 89 |
| NAV-002 | Keyboard app/sidebar operation, explicit active state, 200% zoom, and accessible narrow-layout drawer | P0 | Browser + accessibility | 32, 67, 80, 89 |
| GLB-001 | Grouped local global search with counts/explanations | P1 | Integration + browser | 78 |
| GLB-002 | Dashboard counts agree with canonical state | P1 | Integration + browser | 79 |
| OPS-001 | Long-job progress within target; navigation/search stay usable | P0 | Performance + browser | 80 |
| OPS-002 | Backup/restore canonical round trip and rebuild exclusions | P0 | Integration | 81 |
| OPS-003 | System Status accurate and sanitized | P0 | Integration + browser | 82 |
| SEC-001 | Runtime/private data and credentials never tracked | P0 | Security gate | 83 |
| SEC-002 | SSRF, command, path, CSRF, and XSS controls | P0 | Security suite | 84 |
| SEC-003 | Loopback-only default | P0 | Integration | 85 |
| SEC-004 | Public CI uses synthetic fixtures and no internal network | P0 | CI gate | 86 |
| PERF-001 | Representative corpus targets or approved evidence-based exception | P1 | Performance | 87 |
| OPS-004 | Clean documented setup and first-use smoke | P0 | Clean environment | 88 |

This matrix covers every numbered master acceptance scenario from 1 through 108. A release report must list any test ID that was not run rather than silently omitting it.

## 9. Automated-test organization

Recommended stable suites:

~~~text
tests/unit/                 fast pure behavior and validation
tests/integration/          database, fake SecretStore, mocked Confluence, Git, PDF, FTS
tests/browser/              visible synthetic customer journeys
tests/accessibility/        keyboard, semantics, announcements, automated checks
tests/security/             secret, SSRF, CSRF, XSS, command/path, tracked-file checks
tests/performance/          generated representative corpus and recorded benchmarks
tests/fixtures/             invented deterministic source data only
~~~

Rules:

- Test names include the stable matrix ID where practical.
- Fixtures are deterministic, minimal, and visibly synthetic.
- Freeze/advance time for exact 30-day and relative-date boundaries.
- Use real SQLite/FTS5 for integration behavior.
- Use temporary local Git repositories rather than shell mocks for lifecycle tests.
- Use the fake `SecretStore` for every automated credential test.
- Assert both database state and visible result for important mutations.
- Test jobs across transaction/process boundaries and restart, not only synchronous helpers.
- Record performance fixture size, machine, cold/warm state, repetitions, median, and p95.
- Do not weaken a product security restriction merely to make a fixture easier to run; introduce a test adapter at the explicit dependency boundary.

## 10. Exploratory test charters

Run these time-boxed charters before a complete release. Record observations against stable feature IDs.

| Charter | Time | Mission |
|---|---:|---|
| EX-001 | 30 min | Rapidly change selection, filters, saved views, and search while refresh runs. |
| EX-002 | 30 min | Cancel, reload, terminate, restart, and retry during every long-job phase. |
| EX-003 | 30 min | Enter Unicode, punctuation, FTS operators, HTML-significant text, and long notes/titles/paths. |
| EX-004 | 30 min | Resize, zoom, and keyboard-navigate the neutral chooser, both app sidebars, dense tree, results, preview, settings, and sidebar-drawer transitions. |
| EX-005 | 30 min | Exercise disk-low, worker-unavailable, stale-index, Keychain-unavailable, and connection-loss recovery. |
| EX-006 | 30 min | Attempt unsafe origins, redirects, repository URLs, arguments, paths, symlinks, and file IDs. |
| EX-007 | 20 min | Compare displayed counts/statuses after mixed successes, failures, retries, removals, and recovery. |
| EX-008 | 20 min | Reopen every saved/editing surface and look for leaked secrets or stale unsaved values. |

## 11. Defect reporting contract

Every defect contains:

~~~text
Bug ID:
Title:
Severity: Critical / High / Medium / Low
Journey/test ID:
Build/commit and environment:
Preconditions:
Minimal reproduction steps:
Expected result:
Actual result:
Frequency:
Customer impact:
Sanitized evidence:
Non-sensitive diagnostic/job ID:
Safe workaround, if any:
Owner/status:
Retest build and result:
~~~

Reporting rules:

- One defect describes one root visible problem unless failures are inseparable.
- Use numbered reproduction steps starting from a known state.
- State what the customer sees, not only an internal exception.
- Include expected and actual results even when they seem obvious.
- Attach the smallest redacted evidence that proves the issue.
- Never copy a real PAT, internal URL, document content, personal note, private path, raw database, or full log into a public report.
- For a security/data-loss stop condition, preserve the local evidence, prevent further exposure, and report only a sanitized description publicly.

## 12. Phase acceptance suites

Run the mapped suite before declaring each master-requirements phase complete.

| Phase | Required journeys | Required matrix groups |
|---|---|---|
| 1 — Foundation | CJ-001 partial foundation path, CJ-016 setup/check portion | CFG, SEC-001, SEC-003, SEC-004, OPS-004 foundations |
| 2 — Bookmark core | CJ-001, CJ-002, CJ-003 | CFG, BMK-001–005, SEC-002 relevant cases |
| 3 — Tree/productivity | CJ-004, CJ-007, CJ-017 | BMK-006–020, BMK-015–017 |
| 4 — Refresh/dashboard | CJ-005, CJ-006 | REF, BMK-012–014, GLB-002 relevant dashboard checks |
| 5 — Repository sync | CJ-008, CJ-012 relevant sync path, CJ-018 attribution foundation | REP, GIT, OPS-001 relevant job checks |
| 6 — PDF extraction/search | CJ-009, CJ-011 relevant index path | IDX, SEA |
| 7 — PDF productivity | CJ-010 | PDF |
| 8 — Global/hardening | CJ-012–CJ-016 | GLB, OPS, SEC, PERF and all prior regression suites |

If a later phase changes an earlier feature, rerun its mapped earlier journey. Phase acceptance is cumulative for security, migrations, tracked-file safety, and canonical data preservation.

## 13. Release suites and exit criteria

### 13.1 Pull-request/phase gate

- smallest relevant unit/integration suites pass;
- mapped browser journey checkpoints pass;
- Django system and migration-drift checks pass;
- format/lint checks pass;
- fake-token disclosure scan passes;
- no runtime/private files are tracked;
- affected empty/loading/progress/success/error states are visually reviewed;
- failures have complete defect records and are not described as passed.

### 13.2 Release smoke suite

Minimum smoke:

- CJ-001 secure setup;
- CJ-002 first bookmark;
- CJ-004 core daily bookmark actions;
- CJ-005 refresh/change detection;
- CJ-008 initial repository index;
- CJ-009 keyword-chip search and matched-page open;
- CJ-010 PDF star/note persistence;
- CJ-011 unchanged and incremental sync;
- CJ-013 global search/System Status;
- CJ-014 backup/restore;
- CJ-015 keyboard/accessibility core;
- CJ-016 clean-machine setup.

### 13.3 Release exit criteria

A release is ready only when:

- every P0 journey passes;
- every P1 journey passes or has an explicitly accepted evidence-based exception;
- all master acceptance scenarios 1–108 have a recorded mapped result;
- no open Critical or High security/data-integrity defect remains;
- the complete automated, migration, accessibility, security, secret-scan, and tracked-file checks pass;
- backup/restore and credential re-entry pass;
- representative performance targets pass or have an approved evidence-based exception;
- principal screens and every required state receive a visible browser review;
- clean-machine setup/run instructions are proven;
- test cleanup is verified to affect only disposable test data;
- the release report lists PASS, FAIL, BLOCKED, and NOT RUN counts plus a clear recommendation.

## 14. Reusable Codex QA prompt

~~~text
Read work_prompts/001_OWL_MASTER_REQUIREMENTS.md and
work_prompts/002_FEATURE_TEST_AND_CUSTOMER_JOURNEYS.md completely.

Use only synthetic local fixtures and an isolated fake SecretStore unless I explicitly
authorize a live integration target. Select the customer journeys and stable test IDs
mapped to the implemented phase. Exercise the named customer journeys through the visible
interface and run the required lower-level tests. Record expected and actual results at
each checkpoint.

Continue past non-blocking defects. Stop immediately for suspected secret exposure,
unapproved external access, source writes, destructive behavior, path escape, canonical
data corruption, or data loss. Never inspect or alter the real operating-system credential
store during automated testing.

Finish with:
1. build/environment and fixture summary;
2. PASS, FAIL, BLOCKED, and NOT RUN counts;
3. journey and stable test IDs executed;
4. defects grouped by severity with redacted reproduction steps, expected result,
   actual result, and evidence;
5. security and private-data checks;
6. untested areas and reasons;
7. cleanup performed;
8. release/phase recommendation.
~~~
