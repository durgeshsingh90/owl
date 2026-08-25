# Codex implementation prompt for OWL

- Work-prompt order: 003

## Role

Act as the senior engineer responsible for implementing OWL in this repository.

## Goal

Build the complete local Django application described in:

    work_prompts/001_OWL_MASTER_REQUIREMENTS.md

Validate it through the executable test contract in:

    work_prompts/002_FEATURE_TEST_AND_CUSTOMER_JOURNEYS.md

OWL must provide:

1. A Confluence bookmark manager centered on stable Page ID identity and the real Confluence hierarchy.
2. A fast local search engine for approximately 50 GB of PDFs synchronized from multiple Git/Bitbucket repositories.
3. A shared OWL shell and, in the final phase, one global search across both sources.

All features in the master requirements are part of the target product. The phases describe delivery order, not optional scope.

## Before editing

1. Read the master requirements and feature-test/customer-journey plan completely.
2. Inspect the repository, current branch, existing files, and any AGENTS.md instructions.
3. Identify assumptions that materially affect implementation.
4. Produce a concise phased implementation plan with validation for each phase.
5. Prefer the confirmed defaults in the requirements. Ask only when a missing decision would cause incompatible work or an unsafe external action.

## Implementation boundaries

- Make safe, in-scope local changes and run relevant non-destructive checks without waiting for approval.
- Do not push, publish, deploy, rewrite Git history, or connect to real internal systems unless explicitly requested.
- Never use, invent, request in chat, log, or commit real credentials, internal documents, private repository URLs, database files, exports, or indexed content.
- Use redacted examples, mocks, temporary repositories, and small synthetic PDF fixtures for development and automated tests.
- Do not place secrets in source code, templates, JavaScript, migrations, fixtures, screenshots, or test output.
- Automated credential tests must use an isolated fake `SecretStore`; never inspect or alter the user's real operating-system credential store.
- Never place a real PAT in HTML responses, browser storage, database rows, logs, exports, backups, screenshots, traces, reports, process arguments, or Git-tracked files.
- Preserve user-owned data and unrelated changes.
- Do not silently reduce requirements. If a requirement cannot be completed, document the exact blocker, evidence, impact, and smallest next action.

## Engineering expectations

- Keep the architecture local-first, maintainable, and suitable for one user.
- Use Django templates, HTML, CSS, JavaScript, and Bootstrap rather than adding a SPA framework.
- Keep views thin and place Confluence, Git synchronization, PDF extraction, indexing, import/export, and job logic in explicit service layers.
- Use database migrations for every schema change.
- Use SQLite FTS5 or an equivalently justified local full-text index; never scan all PDFs or use SQL wildcard searches at query time.
- Keep long-running refresh and indexing operations outside normal HTTP request execution and expose durable progress.
- Treat repository clones and indexed data as local runtime data excluded from Git.
- Use accessible, semantic UI patterns and preserve keyboard operation.
- Implement errors and empty/loading/progress states, not only the successful path.

## Verification requirements

For each phase:

1. Map the phase to the stable journey and feature-test IDs in the test contract.
2. Run the smallest relevant automated tests.
3. Run Django system and migration checks.
4. Run configured linting and formatting checks.
5. Exercise the mapped customer journey through the visible browser interface with synthetic data.
6. Record PASS, FAIL, BLOCKED, or NOT RUN for every selected ID.
7. For failures, record severity, redacted reproduction steps, expected result, actual result, and evidence.
8. Continue after ordinary non-blocking failures, but stop immediately for suspected secret exposure, unapproved external access, destructive behavior, path escape, canonical data corruption, or data loss.
9. Verify that no secret, internal URL, PDF content, database, repository clone, credential-store data, or generated runtime data is tracked by Git.
10. For PAT flows, verify the fake token is absent from responses, rendered/browser state, database rows, logs, exports, backups, screenshots, traces, reports, process arguments, and tracked files.
11. Report the commands run, results, cleanup, remaining limitations, and the next phase.

Before declaring the complete product finished, verify every acceptance scenario in the master requirements and every required P0/P1 journey in the test contract. Render and inspect the principal desktop screens, including loading, empty, success, partial-failure, unavailable, configuration, and indexing states.

## Completion response

Lead with what is working. Then report:

- implemented phases and major files;
- journey/test IDs passed, failed, blocked, and not run;
- validation performed, defects by severity, and release/phase recommendation;
- any requirement not completed;
- configuration the user must supply locally;
- exact run and first-use instructions;
- known limitations or follow-up decisions.

Stop only when the current approved phase is genuinely complete or a material blocker requires user input.
