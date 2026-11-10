# Gap register v8 — final

Statuses: NOT STARTED | IN PROGRESS | IMPLEMENTED BUT UNVERIFIED | VERIFIED PASS | BLOCKED | NOT BUILT (known, accepted)
"VERIFIED PASS" means a test in this repository demonstrates it (SQLite and, where noted, PostgreSQL 16). It does **not** mean externally validated.

## Requirements
| ID | Requirement | Status | Evidence / limit |
|---|---|---|---|
| S-1 | Authorization: roles, escalation, cross-user isolation | VERIFIED PASS | `test_authz_matrix` (OpenAPI-driven allowlists; mutation-checked). Report-draft/PDF-artifact isolation not covered |
| S-1c | Row-level tenant isolation | NOT BUILT (decision D6) | verified gap: any authenticated user reads every company; **one organisation per deployment** (`TENANCY.md`) |
| S-2 | Upload validation | VERIFIED PASS | content/magic-byte checks, zip limits, safe download; `test_upload_security`. Only chat attachments + MSME documents; bank-statement import has its own parser |
| S-3 | Prompt-injection defence | PARTIAL | extraction path is data-only and the engine rejects values their citation does not print (tested). Chat tools reading attachments and adversarial corpora against a real model: NOT STARTED |
| S-4 | SQL injection | PARTIAL | `test_sql_injection_probes`: 4,080 probes over 45 GET operations, detector proven by a positive control. POST bodies, chat/MCP tools and the 56 medium bandit findings individually not reviewed |
| S-5 | Dependency/secret scanning in CI | IMPLEMENTED BUT UNVERIFIED | `security.yml` never ran on GitHub. Local: pip-audit 0 findings beyond 2 documented exceptions over 112 packages; bandit 0 high; npm audit 0 |
| S-6 | JWT algorithm pinning | VERIFIED PASS | `test_oauth_alg_pinning`; python-jose/ecdsa advisories accepted with mitigation (`SECURITY_EXCEPTIONS.md`); PyJWT migration NOT STARTED |
| S-7 | PostgreSQL deadlocks in concurrent writes | VERIFIED PASS | retry policy covers deadlocks/serialization failures; 14/25 races crashed before, 0/25 after (`tests/stress_msme_confirm_race.py`) |
| S-8 | Rate limiting / quotas | NOT BUILT | |
| D-1 | Document extraction with field-level citations and confidence | VERIFIED PASS (plumbing) | real docket pipeline + real Tesseract scan; **model accuracy NOT evaluated** (model stubbed; no labelled Indian-invoice set) |
| D-2 | Human correction, immutable original, audited acknowledgement | VERIFIED PASS | append-only history is an application convention, not DB-enforced |
| D-3 | Duplicates, idempotent processing, failure recovery | VERIFIED PASS | threads on SQLite + PostgreSQL; multi-process contention not tested |
| D-4 | Automated off-platform backup of uploaded documents | NOT BUILT | Render disk snapshots are daily (RPO up to 24 h); see `DEPLOYMENT_RENDER.md` |
| F-1 | Invoice-to-bank reconciliation | VERIFIED PASS for 1:1 | explainable scoring; DB-enforced single confirmed match; bank data provably untouched. Split/aggregate payments, TDS logic, ledger posting NOT BUILT; weights untuned on real data |
| C-1 | Persisted, effective-dated GST deadlines | Engine + persistence VERIFIED PASS; rule data IMPLEMENTED BUT UNVERIFIED | secondary-sourced except PMMY 2024 (PIB); `RULE_SOURCES.md` |
| C-2 | Workpapers, maker-checker, audit, reports | VERIFIED PASS | draft preparation is never labelled as filed; audit chain tamper-evident within the database only (anchor the head hash externally) |
| C-3..C-5 | Audit anchoring, notifications, other statutory calendars | NOT BUILT | |
| R-1 | Credit-readiness assessment | VERIFIED PASS | no composite score; requirements are UNVERIFIED_TYPICAL |
| R-2 | Scheme criteria with evidence | Engine/API VERIFIED PASS; rule data IMPLEMENTED BUT UNVERIFIED | **only 2 schemes** (PMMY, CGTMSE) |
| R-3, R-4, R-6 | More schemes; Udyam portal verification; application packaging | NOT BUILT | |
| R-5 | Declared-turnover cross-check | VERIFIED PASS (heuristic only) | |
| O-1 | Health/readiness, structured logs, correlation ids, ops status, scheduled sweep | VERIFIED PASS | `test_msme_ops` (40 checks, SQLite + PostgreSQL). Alerts-as-code NOT BUILT |
| P-1 | Private persistent document storage | VERIFIED PASS (local disk; Azure Blob vs Azurite) | not tested against real Azure; no S3 backend |
| X-1 | Cloud deployment | **BLOCKED** | needs a Render account, a GitHub repository, and the prompted secrets |
| X-2 | Production persistence as code | IMPLEMENTED BUT UNVERIFIED | `render.yaml` (managed PostgreSQL + disk) passes 34 local checks; **Render's own validator not run**. The Azure Terraform under `terraform/` is unchanged (single-replica SQLite demo) |
| X-3 | Container image | IMPLEMENTED BUT UNVERIFIED | entrypoint behaviour VERIFIED (14 checks, root + throwaway user); the Docker build was not run (no Docker) |
| B-1 | Backup/restore tested | VERIFIED PASS (procedure) | `test_msme_restore_drill` on SQLite and PostgreSQL 16: audit chain identical, document checksums, point-in-time, tamper detection. Render's own snapshots/PITR not exercised |
| T-1 | Upstream failing tests | NOT FIXED | 1 genuine Swiss-pack failure + 13 shared-state failures under pytest, unchanged from baseline |
| T-2 | Browser end-to-end | **BLOCKED** | no browser can be downloaded here, and no UI exists for the MSME features |
| U-1 | User interface for MSME features | NOT BUILT | API only |
| L-1 | e-invoice artefacts bundled in the docket wheel (EUPL-1.2 etc.) | OPEN decision | `THIRD_PARTY.md` |

## Measured test totals (final tree)
SQLite (21 script suites, all exit 0, no skips): erp_validation, security_regressions(1), camt_import, oauth_alg_pinning(7), upload_security(23), msme_documents(65),
msme_documents_api(35), msme_docket_integration(23), msme_compliance(70), msme_compliance_api(47), msme_credit(63), msme_credit_api(43), msme_reconciliation(50),
msme_reconciliation_api(33), msme_ops(40), msme_storage(28), render_blueprint(34), docker_entrypoint(14), msme_restore_drill(8), authz_matrix(37), sql_injection_probes(4).
pytest: **246 passed, 1 skipped, 14 failed** (the 14 are the baseline failures; the new unit tests are included in the 246).
PostgreSQL 16, backend asserted: documents, documents_api, docket_integration, compliance, compliance_api, credit, credit_api, reconciliation, reconciliation_api, ops (all pass);
security_regressions, camt_import, erp_validation, storage and restore_drill (SQLite + PostgreSQL, 16 checks) also pass.
Not run on PostgreSQL: authz_matrix, sql_injection_probes, upload_security. Mutation checks were run for every phase's key guarantees (bytecode cleared between runs).
Environment notes: Python 3.12; the tree was rebuilt from the delivered zips after a sandbox reset and reproduced identical results on newer dependency versions.
