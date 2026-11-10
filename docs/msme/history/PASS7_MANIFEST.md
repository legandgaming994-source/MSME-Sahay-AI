# Pass 7 delivery — reconciliation and turnover cross-check (apply on top of Pass 6)

Apply: copy `files/` over the repo root, or `patch -p1 < modified-files.patch` (modified files) and copy the NEW files.
Verified: Pass-6 tree + patch + NEW files reproduces the tested tree byte-for-byte.

## NEW files
| Path | Purpose |
|---|---|
| msme/reconciliation/migrations.py | DDL, migration 43; partial unique indexes: one confirmed match per document and per payment |
| msme/reconciliation/matching.py | pure, explainable scoring + greedy one-to-one assignment (ambiguity flagged) |
| msme/reconciliation/service.py | run / confirm / reject / unmatch / manual / queues / summary; bank data read-only; audited |
| msme/reconciliation/__init__.py | package marker |
| msme/credit/turnover.py | heuristic declared-turnover cross-check (R-5) |
| api/routers/msme_reconciliation.py | REST API (viewer reads; non-demo manager writes) |
| tests/test_msme_matching.py | 14 pure tests |
| tests/test_msme_audit_retry.py | 4 pure tests of the retry policy |
| tests/test_msme_reconciliation.py | 50 checks: real documents + bank rows, DB constraint, race, bank immutability, turnover |
| tests/test_msme_reconciliation_api.py | 33 checks over HTTP |
| tests/stress_msme_confirm_race.py | optional stress tool (not CI): 25 two-way confirmation races per backend |
| docs/msme/RECONCILIATION.md | scoring, lifecycle, concurrency finding, limits |

## MODIFIED files (relative to Pass 6)
| Path | Change |
|---|---|
| msme/compliance/audit.py | **BUG FIX**: `is_retryable` - PostgreSQL deadlocks/serialization failures are retried like unique violations |
| msme/compliance/service.py | uses the shared `is_retryable` (same fix for approvals and other audited writes) |
| msme/compliance/figures.py | extracted `load_approved_documents`, `direction_for`, `party_gstins` (behaviour-preserving; compliance/credit suites unchanged) |
| msme/credit/service.py | +`turnover_check` |
| api/routers/msme_credit.py | +`GET /turnover-check` |
| lambda_erp/database.py | +`_m043_msme_reconciliation`, registry entry 43 |
| api/main.py | include the reconciliation router |
| .github/workflows/ci.yml | reconciliation suites on SQLite and PostgreSQL; new pure tests |
| docs/msme/CREDIT_AND_SCHEMES.md, GAP_REGISTER.md | updated (v7) |

No new runtime dependencies.
