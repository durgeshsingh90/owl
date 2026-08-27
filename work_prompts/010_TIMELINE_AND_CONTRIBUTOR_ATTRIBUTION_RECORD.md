# 010 — Bookmark timeline and contributor attribution record

Date: 2026-08-26

## Delivery status

- **Bookmark timeline: implemented and validated.** Bookmark Manager keeps the real
  Confluence hierarchy and adds a compact **Saved timeline** in its left sidebar.
  Current-year bookmarks are grouped by local month; older bookmarks are grouped by
  year. Search and filters recalculate the timeline without changing stored parent/child
  relationships. The list is bounded and paginated so it does not duplicate a large
  bookmark tree in the page.
- **Bitbucket contributor experience: specified, not yet connected to repository data.**
  The Bitbucket Search foundation reserves the right-side **People & commits** area and
  states the identity roles it will support. Repository synchronization, contributor
  counts, scroll highlighting, and contributor activity ledgers remain Phase 5 work and
  must not be presented as active before that phase is implemented.

## Attribution decision

OWL keeps these roles separate:

| Display role | Authoritative source | Fallback behavior |
|---|---|---|
| Commit author | Git commit metadata | Show the raw Git identity, with email hidden by default |
| Committer | Git commit metadata | Show separately from the author |
| Pushed by | Bitbucket push/ref-change evidence | Show **Unavailable** when authoritative evidence is absent |
| PR created by | Bitbucket pull-request metadata | Show **Unavailable** when PR history was not synchronized |
| Merged by | Bitbucket fulfilled pull-request metadata | Never infer from author, committer, or closer |
| Closed by | Bitbucket non-merge close/decline metadata | Keep separate from **Merged by** |

The indexed branch is the repository's configured/default branch. It may be named
`master`, `main`, or something else; OWL must not hardcode it. A shallow clone exposes
only **Available history**. **Full History** can deepen reachable Git commits, but cannot
reconstruct missing historical push or pull-request evidence. OWL therefore reports
commit-history, push-evidence, and PR-history coverage separately. Its loopback-only
server never exposes a public Bitbucket Cloud webhook receiver; Cloud push evidence needs
a separately approved authenticated relay/import design or remains **Unavailable**.

## Planned contributor interaction

The desktop Bitbucket results view uses a sticky right rail of contributor names and
role-specific counts. As PDF results or changes scroll, the contributor associated with
the most visible result receives `aria-current` without moving keyboard focus. Selecting
a person opens or filters to all locally known activity for that person, including commit
hash, subject, date, repository, branch, role, and affected PDF links. A narrow viewport
uses a named drawer or compact strip.

Authored/Committed counts deduplicate by repository and commit hash, Pushed counts use
authoritative branch-update events, and PR counts deduplicate by repository and PR ID.
Only fulfilled PRs count as merged; closing or declining an unmerged PR is a separate role.

## Requirement and test traceability

- Master requirements: sections 15.4, 22.7, 25.6 and acceptance scenarios 101–108.
- Customer journeys: CJ-017 and CJ-018.
- Stable tests: BMK-019, BMK-020 and GIT-001 through GIT-006.

## Evidence recorded for this change

- Timeline grouping, semantic rendering, filtering, and hierarchy preservation have
  automated coverage.
- A desktop browser check confirmed the timeline, canonical hierarchy reveal from a
  previously collapsed branch, keyboard focus, the planned People & commits panel, and
  absence of horizontal overflow or browser console errors.
- The complete local suite passed with 369 tests, plus formatting, lint, Django system,
  migration-drift, and public-repository safety checks.
- The People & commits panel remains deliberately labelled as planned until repository
  synchronization exists.

Reference behavior is grounded in the official
[Git log documentation](https://git-scm.com/docs/git-log),
[Bitbucket Cloud commits API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-commits/),
[Bitbucket Cloud pull-request API](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/),
[Bitbucket Cloud event payloads](https://support.atlassian.com/bitbucket-cloud/docs/event-payloads/),
[Bitbucket Data Center pull-request API](https://developer.atlassian.com/server/bitbucket/rest/v819/api-group-pull-requests/),
and [Bitbucket Data Center repository API](https://developer.atlassian.com/server/bitbucket/rest/v809/api-group-repository/).
