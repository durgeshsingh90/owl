# 006 — Phase 3 validation report

Date: 2026-08-25
Implementation contract: `001_OWL_MASTER_REQUIREMENTS.md`, Phase 3
Acceptance contract: `002_FEATURE_TEST_AND_CUSTOMER_JOURNEYS.md`
Delivery phase: Phase 3 — Bookmark tree and productivity
Result: **PASS for the defined Phase 3 scope**

## 1. Delivered scope

Phase 3 now provides:

- a responsive real-ancestor bookmark tree with hierarchy-only context nodes, persisted
  expansion, selection, checked-item, and scroll state, Expand All, Collapse All, and
  search-driven reveal without changing the stored hierarchy;
- separate availability, calculated NEW/UPDATED/NORMAL recency, changed-since-viewed,
  relative date, exact timestamp, and usage presentation;
- local search across title, OWL number, Page ID, URL, space, author, creator, modifier,
  tags, notes, and complete breadcrumb paths without a Confluence request;
- combinable favorite, pin, ALL-tag, person, space, availability, recency,
  changed-since-viewed, date, broken/inaccessible, and open-count filters with active
  chips, live result counts, clear actions, and shortcut views;
- tree-preserving default browsing plus explicit flat result mode, breadcrumb context,
  and the required date, title, author, favorite, pin, open-count, viewed, and refreshed
  sorts;
- case-insensitive saved views that restore validated search, filters, and sort while
  deliberately excluding transient selection and expansion; the saved-view schema also
  retains its explicit column-settings payload;
- escaped local plain-text notes, quick notes, normalized case-insensitive tags,
  independent favorite and pin actions, exact Page ID/breadcrumb/URL copy controls, and
  AJAX feedback through the permanent status region;
- OWL-only open tracking with atomic open count, first-opened, last-viewed, and viewed
  version updates after a validated external open;
- versioned, integrity-checked, credential-free JSON export and record-by-record current
  or heterogeneous legacy JSON import with durable sanitized partial-failure results;
- confirmed local deletion that cannot delete from Confluence and prunes only unshared,
  empty hierarchy nodes;
- semantic tree/disclosure behavior, keyboard tree navigation and shortcuts, focusable
  exact dates, accessible pressed/selected/expanded state, and text/icon status that does
  not rely on color alone.

The stable Phase 2 Confluence identity, source-metadata ownership, secure profile, URL
validation, and open boundary remain in place. Phase 3 adds only OWL-owned personal and
discovery behavior around those boundaries.

## 2. Automated validation evidence

The final release gate is:

```text
./scripts/check.sh
```

| Check | Result |
|---|---|
| Universal dependency lock | PASS — 27 packages resolved from the unchanged lock |
| Locked environment synchronization | PASS — 19 packages checked |
| Public-repository safety scan | PASS — 101 indexed or untracked, non-ignored candidates |
| Formatting | PASS — 74 files already formatted |
| Ruff code quality | PASS |
| Django system checks | PASS — no issues |
| Migration drift | PASS — no changes detected |
| Synthetic automated tests | PASS — 356 passed |
| Branch-aware coverage | PASS — 81.2% total |
| Existing local migration application | PASS — migration 0003 applied; no operations pending |
| Git whitespace validation | PASS |

Focused evidence completed before the aggregate gate includes:

- **37 passing query-service tests** for every search scope, AND/OR group rules, exact
  recency boundaries, dates, saved-view serialization, dynamic counts, all declared
  sorts, and non-mutating ancestor context;
- **24 passing integrated HTTP/UI tests** for notes/tags, favorite/pin, search, combined
  filtering, flat sorting, saved views, export, partial import, local delete, CSRF,
  method restrictions, loopback-only actions, and keyboard/ARIA/data hooks;
- model and import/export suites covering Unicode-aware tag identity, independent
  personal state, saved-view identity, import progress/failures/provenance, deterministic
  idempotent merge, integrity verification, secret-free round-trip, and shared-tree
  deletion;
- Django migration drift and system checks passing during focused Phase 3 validation.

All automated data is invented and isolated under the test runtime root. Tests use the
in-memory credential store and do not contact a live Confluence or Bitbucket system.

## 3. Visible customer-interface evidence

The Phase 3 customer journeys were exercised with synthetic bookmarks and credentials in
the local OWL interface. No private page, real PAT, or external customer system was used.

| Checkpoint | Result |
|---|---|
| Responsive three-pane workspace at desktop width | PASS |
| 390×844 phone layout after the responsive overflow repair | PASS — no horizontal page overflow |
| Search by title, Page ID, URL, space, person, breadcrumb, tag, and note | PASS |
| Combined filters, active chips/count, clear, flat sort, and breadcrumb context | PASS |
| Save a named view, change controls, and restore the exact saved query | PASS |
| Add/edit notes and tags through details and quick-note actions | PASS — escaped and locally searchable |
| Favorite and pin independently with immediate persisted feedback | PASS |
| Expansion, selection, and scroll state survive local updates/reload | PASS |
| `/`, arrows, E, F, P, and Enter keyboard journeys | PASS |
| Copy Page ID and complete breadcrumb | PASS — exact clipboard values |
| Safe external-tab open | PASS — one validated tab with no-referrer/no-store protections |
| OWL open tracking | PASS — visible count advanced from 12 to 13 with viewed state updated |
| Partial import | PASS — one valid record imported and one rejected with a sanitized reason |
| Export JSON | PASS — browser download completed with versioned bookmark data and no credential |
| Browser warning/error console | PASS — empty |

The first phone-width run exposed workspace overflow caused by desktop tree minimums.
The responsive grid/tree rules were tightened, and both desktop and phone journeys were
repeated successfully. No screenshot, export, database, or browser trace was saved to the
public repository.

## 4. Phase 3 journey and feature-ID traceability

`PASS` means the Phase 3 behavior is implemented and exercised. `DEFERRED` marks work
that the master implementation plan assigns to Phase 4 even where the broader test matrix
lists a related ID in the Phase 3 acceptance row.

| Journey or test ID | Status | Evidence and boundary |
|---|---|---|
| CJ-004 | PASS | Visible and automated daily finding/organization covers tree state, all local search scopes, combined filters, saved views, notes/tags, favorite/pin, exact copy, OWL open tracking, recent/frequent/never views, flat sorts, keyboard control, and status announcements. |
| CJ-007 | PASS | Heterogeneous import continues after bad records, reports sanitized totals/failures, is idempotent, preserves existing OWL-owned values, exports a credential-free versioned backup, re-imports it, and deletes only confirmed local data. |
| BMK-006 | PASS | Availability priority, inclusive NEW/UPDATED duration boundaries, NORMAL fallback, changed-since-viewed separation, relative labels, and focusable exact timestamps pass. |
| BMK-007 | PASS | Title, URL, Page ID, metadata, tag, note, and breadcrumb search returns the real node plus all ancestors and selects/reveals a unique result. |
| BMK-008 | PASS | Different filter groups combine with AND, selected tags require ALL, active chips/counts render, and context ancestors remain understandable. |
| BMK-009 | PASS | Every global date/title/usage sort uses flat results with breadcrumbs; database parent mappings remain unchanged. |
| BMK-010 | PASS | Notes are normalized plain text, escaped in HTML, updated asynchronously, locally searchable, imported/exported, and protected from source refresh ownership. |
| BMK-011 | PASS | Favorite and pin toggle independently, provide immediate accessible state/status, and persist across reload and source-owned updates. |
| BMK-012 | PASS for Phase 3 | Validated OWL opens atomically update count, first/last viewed dates, and viewed version; changed-since-viewed display passes. Producing the later source-version transition through refresh belongs to Phase 4. |
| BMK-013 | DEFERRED to Phase 4 | Refresh Selected requires the durable refresh execution introduced by the master plan's Phase 4 boundary. Phase 3 multi-selection persists but does not claim a refresh job. |
| BMK-014 | DEFERRED to Phase 4 | Refresh All progress, rate limiting, retries, and partial-failure recovery require Phase 4 durable refresh runs/items and dashboards. |
| BMK-015 | PASS | Legacy/current import validates records independently, continues after failures, assigns deterministic identities, remains idempotent, and applies documented ownership rules. |
| BMK-016 | PASS | Versioned export contains supported bookmark/personal/hierarchy state, an integrity digest, and no configuration, PAT, authorization header, cookie, session, or credential-store data; round-trip passes. |
| BMK-017 | PASS | Confirmation is required; deletion removes only the selected OWL bookmark/personal state and prunes only its now-empty unshared branch while preserving source/shared siblings. |
| BMK-018 | PASS | Semantic tree roles/levels, disclosure and pressed/selected state, keyboard search/navigation/actions, accessible exact dates, copy controls, confirmations, and polite status announcements pass. |

BMK-006–011 and BMK-015–018 therefore pass in full for Phase 3. BMK-012's delivered
open/viewed behavior also passes. BMK-013/014 and the refresh portions of CJ-005/CJ-006
are deliberately not claimed early.

## 5. Security and privacy statement

- Notes and tags remain local OWL-owned values and are never sent to Confluence.
- Templates escape untrusted title, hierarchy, note, tag, search, import, and saved-view
  values; AJAX presentation writes personal text through safe DOM text operations.
- Every Phase 3 state change is POST-only, CSRF-protected, and loopback-only by default.
- Import is explicitly uploaded, UTF-8/size/schema/count bounded, integrity checked when
  applicable, processed record by record, and reports only sanitized failures.
- Export uses an explicit field allowlist, attachment/no-store/no-referrer headers, and
  excludes configuration, PATs, credentials, authorization, cookies, and sessions.
- External page and parent opens revalidate the configured origin; successful opens use
  no-referrer/no-store response protections and failed validation does not increment usage.
- Delete requires explicit confirmation and has no Confluence client or source-delete
  capability.
- No real secret, private URL, customer page, screenshot, export, database, or browser
  trace was added to source, and no live external integration was invoked.
- No commit, push, deployment, or live source mutation was performed by this phase.

## 6. Migrations and operations

Migration `bookmark_manager.0003_phase3_bookmark_productivity` adds normalized tags,
saved bookmark views, durable import runs/failures, import provenance, personal-state
indexes, and the supporting Phase 3 constraints. Focused migration drift checks report
no additional model changes. Before applying it to the normal local database, OWL created
the ignored online SQLite backup `var/backups/owl-pre-phase3-2026-08-25.sqlite3` and
verified its integrity. Migration 0003 then applied successfully, the upgraded database
passed its integrity check, and the final migration plan is empty.

Phase 3 remains a single-process local application. Its search, filters, personal
organization, import, export, and deletion are local synchronous operations; durable
background workers are intentionally introduced with Phase 4 refresh jobs.

## 7. Deliberate Phase 4 and later boundaries

Phase 3 does not claim Refresh One, Refresh Selected, Refresh All, durable refresh runs
and items, progress dashboards, rate-limit scheduling, retry/resume, partial refresh
failure recovery, rename/move observation through a live refresh, or a refreshed source
version causing the changed-since-viewed transition. These are Phase 4 — Bookmark refresh
and dashboards in the master implementation order.

The overlapping BMK-013/014 entries in the Phase 3 acceptance-matrix row do not override
that explicit implementation boundary. Their prerequisites do not exist until Phase 4,
so recording them as deferred is more accurate than a partial or synthetic pass.

Repository synchronization, PDF extraction/search/preview, global search, complete
backup/restore, packaged release, and representative-corpus performance remain assigned
to Phases 5 through 8.

## 8. Next implementation phase

Proceed with **Phase 4 — Bookmark refresh and dashboards**: durable refresh runs/items,
Refresh One/Selected/All, bounded concurrency and rate-limit handling, retry/resume,
progress and failure surfaces, source rename/move/version transitions, changed-since-viewed
refresh proof, recently changed, and broken/inaccessible recovery dashboards. Preserve all
Phase 3 OWL-owned notes, tags, favorite, pin, saved-view, usage, import, and hierarchy state
through every source-owned refresh transition.
