# 007 — Two-app navigation validation report

Date: 2026-08-25
Requirements: `001_OWL_MASTER_REQUIREMENTS.md`, section 7.1
Journey coverage: `002_FEATURE_TEST_AND_CUSTOMER_JOURNEYS.md`, NAV-001, NAV-002, EX-004
Result: **PASS**

## 1. Delivered navigation contract

OWL now presents exactly two primary application choices everywhere:

1. **Bookmark Manager**
2. **Bitbucket Search**

The OWL mark links to the neutral two-app chooser and is not presented as a third
application. Dashboard, Global Search, Repositories, and System Status are absent from
the primary application navigation. Existing URLs remain available so bookmarks and
later implementation work do not break.

## 2. App-owned left panels

Bookmark Manager owns a labelled left panel containing its browse views, favorites,
pins, recent/frequent/never-viewed shortcuts, JSON import/export, Confluence settings,
and System Status. Its existing configuration gear remains visible in the main toolbar
and still opens the PAT settings journey.

Bitbucket Search owns a separate labelled left panel containing PDF Search,
Repositories, Index and Refresh Status, and System Status. Repository and indexing
functions are no longer presented as peer applications.

Every app route identifies both the active application and the active function with
accessible current-page state. Shared diagnostics remain utilities rather than a third
workspace.

## 3. Responsive and accessibility behavior

At desktop width, each app uses a stable 270-pixel left panel and a flexible content
area. At phone width, the same panel becomes a full-width app-menu button so application
content is reachable without first scrolling through a long sidebar. The button exposes
its expanded state, controls the named panel, has a 44-pixel minimum target, and closes
with Escape while returning focus to the button.

The primary navigation, sidebar landmarks, current-page indicators, skip link, permanent
status region, and settings control retain accessible names. Neither tested layout has
horizontal page overflow or duplicate element IDs.

## 4. Automated evidence

The complete release gate passed:

```text
PATH=/Users/durgesh/Projects/owl/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin ./scripts/check.sh
```

| Check | Result |
|---|---|
| Dependency lock and locked environment | PASS — 27 packages resolved; 19 checked |
| Public-repository safety scan | PASS — 105 candidate files including this report |
| Formatting | PASS — 76 files already formatted after this report was added |
| Ruff quality checks | PASS |
| Django system checks | PASS — no issues |
| Migration drift | PASS — no changes detected |
| Automated tests | PASS — 361 passed |
| Branch-aware coverage | PASS — 81.1% total |

The focused page, Phase 2, Phase 3, and System Status suites also passed independently:
**67 passed**. Page tests enforce the exact two-link primary navigation, app-specific
sidebar route matrix, single active function, responsive menu control relationship, and
continued availability of the Confluence settings gear.

## 5. Visible interface evidence

The local interface was operated with an isolated temporary database and two invented
bookmarks. No private URL, PAT, repository, or external service was used.

| Checkpoint | Result |
|---|---|
| Neutral launcher primary choices | PASS — exactly Bookmark Manager and Bitbucket Search |
| Bookmark Manager desktop panel | PASS — visible at 270 px with All Bookmarks active |
| Bookmark favorites shortcut | PASS — route and active state updated correctly |
| Bitbucket Search desktop panel | PASS — visible at 270 px with Search PDFs active |
| Repositories and Index Status routes | PASS — correct app and function remained active |
| Bookmark Manager at 390×844 | PASS — menu collapsed by default, opened on demand, closed with Escape |
| Bitbucket Search at 390×844 | PASS — menu collapsed by default and opened on demand |
| Horizontal page overflow | PASS — zero at tested desktop and phone widths |
| Duplicate element IDs | PASS — none on tested app pages |
| Browser warning/error log | PASS — empty |

The visible pass caught and corrected a native disclosure behavior that reserved an empty
desktop column while suppressing its contents. The final menu uses an explicit accessible
button: the left panel is always visible on desktop and is deliberately collapsible only
on narrow screens.

## 6. Operational statement

This change does not add or alter database models, migrations, credentials, repository
access, or external integrations. It does not remove existing routes. No commit, push,
deployment, or live Confluence/Bitbucket action was performed.
