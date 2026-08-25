# 005 — Phase 2 validation report

Date: 2026-08-25
Implementation contract: `001_OWL_MASTER_REQUIREMENTS.md`, Phase 2
Acceptance contract: `002_FEATURE_TEST_AND_CUSTOMER_JOURNEYS.md`
Delivery phase: Phase 2 — Bookmark Manager core
Result: **PASS for the defined Phase 2 scope**

## 1. Delivered scope

Phase 2 now provides:

- a persistent **Confluence settings** gear with a responsive dialog and dedicated
  no-JavaScript fallback page;
- masked PAT entry, current-input Show/Hide, Cancel clearing, explicit read-only
  connection testing, secure save/replace/remove, actionable connection states, and
  complete environment-profile precedence;
- a versioned credential-store envelope bound to the canonical Confluence origin and
  authentication mode, so even a database plus compensation failure cannot pair a
  replacement credential with the retained previous origin;
- strict origin, DNS, context-path, Page ID, redirect, timeout, response-size, and
  same-origin validation around a Bearer-authenticated read-only Confluence adapter;
- raw Page ID, modern page URL, and legacy `viewpage.action` input support;
- permanent OWL numbers and canonical Confluence Page ID deduplication before a source
  request;
- transactional source-metadata and hierarchy upserts, hierarchy-only ancestors,
  shared-ancestor reuse, and rename/move handling without stale empty branches;
- a searchable local hierarchy, selected-page details, breadcrumb, source metadata,
  non-blocking similar-title warnings, and first-use/error/empty states;
- server-validated POST-only Confluence opens with no-referrer/no-store behavior and
  atomic usage tracking;
- CSRF-protected asynchronous drawer actions that also work in sandboxed local browser
  surfaces without weakening Django's origin checks;
- a deterministic loopback-only Confluence fixture for visible acceptance without a
  real service or credential.

## 2. Automated validation evidence

The final release gate is:

```text
./scripts/check.sh
```

Final result:

| Check | Result |
|---|---|
| Universal dependency lock | PASS — 27 packages resolved from the unchanged lock |
| Locked environment synchronization | PASS — 19 packages checked |
| Public-repository safety scan | PASS — 89 indexed or untracked, non-ignored candidates |
| Formatting | PASS — 64 files already formatted |
| Ruff code quality | PASS |
| Django system checks | PASS — no issues |
| Migration drift | PASS — no changes detected |
| Synthetic automated tests | PASS — 277 passed |
| Branch-aware coverage | PASS — 82.4% total |
| Existing local migration application | PASS — no migrations pending |
| Git whitespace validation | PASS |

The suite covers configuration, secret storage, origin validation, the HTTP adapter,
bookmark identity/hierarchy, HTTP views, templates, redaction, logging, runtime
settings, system status, and public-repository safety. All automated connection tests
use an isolated fake `SecretStore` and invented source responses; normal checks cannot
contact a live Confluence or Bitbucket system.

Important regression cases include:

- the rendered settings form supplies a working CSRF token to the visible drawer;
- connection testing never persists form values;
- a stored PAT is never returned to a form, response, representation, session, or
  database row;
- verification receipts are opaque, credential-bound, origin-bound, one-use values;
- origin changes require an explicitly submitted replacement PAT;
- database, credential-store, and double-compensation failures preserve or fail closed
  instead of silently mixing profiles;
- raw, malformed, wrong-version, wrong-origin, and wrong-auth stored envelopes cannot
  reach the Confluence adapter;
- duplicate Page IDs short-circuit before client construction or a source request;
- invalid/cross-origin input cannot create a partial bookmark;
- unsafe redirects, SSRF targets, excessive/malformed responses, CSRF failures, and
  untrusted HTML are rejected or escaped;
- the public-source scanner distinguishes Python expressions from real embedded values
  while still rejecting literal credentials, internal endpoints, credential URLs, and
  private/runtime artifacts.

## 3. Visible customer-interface evidence

OWL was run with a fresh temporary database, an in-memory credential store, and the
loopback-only synthetic Confluence fixture. No real Keychain item or external service
was used.

| Checkpoint | Result |
|---|---|
| First-use state, exact **Confluence settings** name, and gear availability | PASS |
| Keyboard Enter opens settings; Escape closes and returns focus to the gear | PASS in Chrome |
| Settings heading receives logical focus | PASS |
| PAT defaults to password type with spellcheck off and replacement autocomplete | PASS |
| Show/Hide affects only current input; Cancel clears unsaved URL/PAT and resets masking | PASS |
| Test Connection gives one sanitized Connected result without saving | PASS |
| Reload after test-only remains Not configured | PASS |
| Test plus Save becomes Connected | PASS |
| Reopen shows **Stored securely**, an empty replacement field, and no value attribute | PASS |
| Complete environment profile says **Managed externally**, returns no values, disables test/save/replace, and offers no remove action | PASS |
| First modern URL save creates OWL #1 and reveals the selected highlighted node | PASS |
| Hierarchy-only ancestors, breadcrumb, space, people, version, dates, URL, and zero initial opens render | PASS |
| Raw and legacy duplicates both reveal OWL #1 and keep the saved count at one | PASS |
| Similar-title second Page ID creates OWL #2 with a non-blocking link to OWL #1 | PASS |
| Search by title, URL, and Page ID reveals the correct node with ancestor context | PASS |
| Invalid text and a wrong-origin URL show safe recovery messages and create no row | PASS |
| Desktop width and 390×844 phone width have no horizontal overflow | PASS |
| Phone settings panel occupies the viewport cleanly with all actions reachable | PASS |
| `/` focuses bookmark search | PASS in Chrome |
| Browser warning/error console | PASS — empty |

During the first visible run, the settings drawer exposed a missing-CSRF-token defect in
an isolated template include. The include was corrected, a rendered-token regression was
added, and the complete visible setup/save journey passed afterward. Django's strict
origin verification was retained; local asynchronous form enhancement handles sandboxed
browser origins rather than allowing a `null` origin.

No screenshot was saved to the public repository. Ephemeral visual frames were inspected
at desktop and phone sizes, then discarded with the temporary database and log.

## 4. Phase 2 journey and feature-ID traceability

`PASS` means the complete Phase 2 portion was exercised. Later-phase behavior sharing a
broader feature area is not implied.

| Journey or test ID | Status | Evidence |
|---|---|---|
| CJ-001 | PASS for Phase 2 | Visible first use, cancel, test-only reload, verified save, secure reopen, keyboard focus, and managed environment pass. Failure mapping, atomic replacement, confirmed fake-store removal, unavailable store, and incomplete environment states pass in integration tests. |
| CJ-002 | PASS | Visible modern-URL save, one OWL identity, hierarchy reveal, full details, status announcement, and three search scopes pass; invalid and wrong-origin branches leave no partial row. |
| CJ-003 | PASS | Visible raw/legacy duplicate reveal and separate similar-title Page ID pass; service tests prove no duplicate source request. |
| CFG-001–CFG-007 | PASS | Gear/dialog accessibility, first-use state, strict origin validation, mask/cancel, explicit non-persistent test, fake-store save/reopen, and no redisplay pass. |
| CFG-008 | PASS | 401, 403, 404 context, 429, timeout, TLS, connectivity, 5xx, oversized, and malformed results are distinctly sanitized in adapter/integration tests. |
| CFG-009–CFG-011 | PASS | Origin-bound PAT replacement, atomic/fail-closed compensation, confirmed secure removal, and local bookmark preservation pass. |
| CFG-012–CFG-013 | PASS | Complete/incomplete environment precedence and unavailable/failed secure-store recovery pass with no plaintext fallback. |
| CFG-014 | PASS for all Phase 2 surfaces | Per-run synthetic canaries are absent from HTML, URL, session, response, SQLite, logs, tracked files, and forbidden representations. Future export/backup surfaces remain assigned to later phases. |
| BMK-001 | PASS | Modern URL produces one immutable Page ID identity and permanent OWL number. |
| BMK-002 | PASS | Raw, modern, and legacy duplicate forms reveal the existing number before any source request. |
| BMK-003 | PASS | Similar titles with distinct Page IDs remain distinct and linked by a warning. |
| BMK-004 | PASS | Shared ancestors are reused; hierarchy-only nodes never receive OWL numbers. |
| BMK-005 | PASS | Same-identity rename/move updates in place and prunes only unshared empty branches. |
| SEC-002 relevant cases | PASS | SSRF/DNS/redirect, CSRF, method, local-action, XSS, response-bound, and no-shell-command boundaries pass. |

The basic Phase 2 open workflow is also covered: only POST is accepted, the saved URL is
revalidated against the active canonical origin, unsafe opens remain local, and a safe
redirect records first/last/open-count/viewed-version state with no-store and no-referrer
headers.

## 5. Security and privacy statement

- No real PAT, internal endpoint, Confluence document, customer title, private repository,
  PDF, screenshot, export, database, log, or index was added to public source.
- Automated tests never use the real operating-system credential store.
- Visible checks used only isolated temporary runtime roots and an in-memory credential
  store; both temporary profiles were removed after exact-scope inspection.
- The adapter makes bounded `GET` requests only and follows redirects only within the
  configured canonical application origin.
- UI-managed credentials are stored only as origin/auth-bound envelopes inside the
  configured secure store. SQLite contains no credential or credential-store identifier.
- No commit, push, deployment, or live external connection was performed.

## 6. Migrations and operations

Migration `bookmark_manager.0002_bookmark_domain` adds the normalized page hierarchy and
bookmark domain. It has been applied to the existing local OWL database, migration drift
is empty, and rerunning migration reports no pending work.

The root README now documents the Phase 2 setup, settings gear, secure PAT behavior, and
bookmark save flow. Background workers remain unnecessary until the durable refresh and
repository-indexing phases.

## 7. Deliberate later-phase boundaries

Phase 2 does not claim the Phase 3 notes, tags, favorite/pin actions, saved views, advanced
filters/sorts, JSON import/export, or bulk refresh dashboards. It also does not claim Git
repository synchronization, PDF extraction/search/preview, global search, backup/restore,
or representative-corpus performance acceptance.

A real Confluence Data Center connection remains an explicit user action through the
finished local settings gear. Live confirmation is intentionally not part of the synthetic
release gate.

## 8. Next implementation phase

Proceed with **Phase 3 — Bookmark tree and productivity**: persisted expansion/reveal state,
date/status rules, advanced filters and saved views, notes, tags, favorites, pins, broader
usage views, and JSON import/export. Reuse the stable Phase 2 Page ID identity, hierarchy,
secure profile, and adapter boundaries rather than replacing them.
