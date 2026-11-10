# Pass 6 delivery — credit readiness and scheme navigation (apply on top of Pass 5)

Apply: copy `files/` over the repo root, or `patch -p1 < modified-files.patch` (modified files) and copy the NEW files.
Verified: Pass-5 tree + patch + NEW files reproduces the tested tree byte-for-byte.

## NEW files
| Path | Purpose |
|---|---|
| msme/credit/migrations.py | DDL for 2 tables, migration 42 (portable SQLite/PostgreSQL) |
| msme/credit/facts.py | registry of settable facts with validation; Udyam format; `msme_category` derived-only |
| msme/credit/readiness.py | evidence engine: bank-statement coverage, GST history, invoice coverage, attestations |
| msme/credit/service.py | append-only facts, derived classification, scheme assessment, immutable audited snapshots, summary |
| msme/credit/report.py | PDF application-preparation summary (escaped; not-an-application banner) |
| msme/credit/__init__.py | package marker |
| api/routers/msme_credit.py | REST API (viewer reads; non-demo manager writes) |
| tests/test_msme_credit.py | 63 checks, real bank imports / compliance tasks / approved documents |
| tests/test_msme_credit_api.py | 43 checks over HTTP |
| docs/msme/CREDIT_AND_SCHEMES.md | model, semantics, limits, what is NOT verified |

## MODIFIED files (relative to Pass 5)
| Path | Change |
|---|---|
| msme/rules/engine.py | `CriterionResult.fact_key` (optional trailing field, backward compatible) so "what to provide next" is exact |
| msme/compliance/audit.py | +`run_in_transaction` / `AuditBusy` shared by services that write audited changes |
| lambda_erp/database.py | +`_m042_msme_credit_readiness`, registry entry 42 |
| api/main.py | include the credit router |
| .github/workflows/ci.yml | credit suites on SQLite and PostgreSQL |
| docs/msme/GAP_REGISTER.md | v6 |

No new runtime dependencies.
