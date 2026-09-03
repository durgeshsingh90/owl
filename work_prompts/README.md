# OWL work prompts

This folder is the version-controlled source of truth for planning and implementing OWL.

## Files

- [001_OWL_MASTER_REQUIREMENTS.md](001_OWL_MASTER_REQUIREMENTS.md) contains the complete product, functional, technical, UX, security, testing, and acceptance requirements.
- [002_FEATURE_TEST_AND_CUSTOMER_JOURNEYS.md](002_FEATURE_TEST_AND_CUSTOMER_JOURNEYS.md) turns those requirements into executable customer journeys, stable feature-test IDs, evidence rules, and release criteria.
- [003_CODEX_IMPLEMENTATION_PROMPT.md](003_CODEX_IMPLEMENTATION_PROMPT.md) is the execution prompt to give Codex after opening this repository.
- [004_PHASE_1_VALIDATION_REPORT.md](004_PHASE_1_VALIDATION_REPORT.md) records the implemented Phase 1 scope, automated and visible evidence, traceability, and deliberate later-phase exclusions.
- [005_PHASE_2_VALIDATION_REPORT.md](005_PHASE_2_VALIDATION_REPORT.md) records the secure Bookmark Manager core, synthetic acceptance evidence, security traceability, and later-phase boundaries.
- [006_PHASE_3_VALIDATION_REPORT.md](006_PHASE_3_VALIDATION_REPORT.md) records the Bookmark Manager tree/productivity and import/export scope, automated and visible customer-journey evidence, feature-ID traceability, and the deliberate Phase 4 refresh boundary.
- [007_TWO_APP_NAVIGATION_VALIDATION_REPORT.md](007_TWO_APP_NAVIGATION_VALIDATION_REPORT.md) records the exact two-app navigation contract, app-owned left panels, responsive menu behavior, and automated and visible validation evidence.
- [008_TROPICAL_HOME_THEME_VALIDATION_REPORT.md](008_TROPICAL_HOME_THEME_VALIDATION_REPORT.md) records the Home navigation update, original tropical visual direction, hierarchy visibility, and validation evidence.
- [009_OWL_THEME_VALIDATION_REPORT.md](009_OWL_THEME_VALIDATION_REPORT.md) records the final knowledge-owl identity, moonlit palette, hierarchy continuity, and validation evidence.
- [010_TIMELINE_AND_CONTRIBUTOR_ATTRIBUTION_RECORD.md](010_TIMELINE_AND_CONTRIBUTOR_ATTRIBUTION_RECORD.md) records the implemented bookmark timeline, truthful source-control identity model, planned contributor rail, and phase boundary.
- [011_ADAPTIVE_PDF_PIPELINE_IMPLEMENTATION_PROMPT.md](011_ADAPTIVE_PDF_PIPELINE_IMPLEMENTATION_PROMPT.md) defines the ETA-first dashboard and repository queue lifecycle, durable retry/pause/notification/resume recovery, background execution contract, and benchmark-gated 80%-budget adaptive concurrency for OWL's PDF extraction and SQLite publication pipeline.

## Recommended use

1. Open the OWL repository in Codex.
2. Give Codex the implementation prompt in this folder.
3. Ask Codex to read the master requirements and feature-test/customer-journey plan completely before editing.
4. Let Codex implement the work in the documented phases and run the mapped journey/test IDs before completing each phase.
5. Supply a real Confluence PAT through the Bookmark Manager's **Confluence settings** gear, which stores it in the operating-system credential store, or through a complete ignored environment profile for development/automation.
6. Keep real Confluence and Git/Bitbucket details, screenshots, logs, test evidence, documents, databases, and indexes outside Git.

The master requirements are authoritative for product and security behavior. The feature-test/customer-journey file is authoritative for validation execution. If a later decision changes the product, update both documents together so implementation and testing remain aligned.
