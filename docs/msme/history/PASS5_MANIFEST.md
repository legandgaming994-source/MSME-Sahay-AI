# Pass 5 delivery — compliance workspace (apply on top of Pass 4)

Apply: copy `files/` over the repo root, or `patch -p1 < modified-files.patch` (modified files) and copy the NEW files.
Verified: Pass-4 tree + patch + NEW files reproduces the tested tree byte-for-byte.

## NEW files
| Path | Purpose |
|---|---|
| msme/compliance/migrations.py | DDL for 5 tables, migration 41 (portable SQLite/PostgreSQL) |
| msme/compliance/service.py | profile, overrides, idempotent task generation, workpaper lifecycle, maker-checker, filing record |
| msme/compliance/figures.py | period figures from APPROVED documents only, with an explicit attention list |
| msme/compliance/audit.py | SHA-256 hash-chained audit trail + verification |
| msme/compliance/report.py | PDF workpaper + CSV calendar (HTML-escaped, URL fetch denied, CSV-injection safe) |
| msme/compliance/data/checklists.json | internal-guidance preparation checklists (labelled not-official) |
| msme/compliance/__init__.py | package marker |
| api/routers/msme_compliance.py | REST API (viewer reads; non-demo manager writes) |
| tests/test_msme_compliance.py | 70 checks incl. concurrency, tamper detection, PDF text |
| tests/test_msme_compliance_api.py | 47 checks: roles and the two-manager flow over HTTP |
| docs/msme/COMPLIANCE_WORKSPACE.md | lifecycle, limits, what is NOT verified |

## MODIFIED files (relative to Pass 4)
| Path | Change |
|---|---|
| msme/documents/service.py | **BUG FIX**: group-based duplicate reconciliation (original no longer flagged as a duplicate of its copy); microsecond `created_at` |
| tests/test_msme_documents.py | +10 duplicate-group regression checks (6 fail on the Pass 4 code) |
| lambda_erp/database.py | +`_m041_msme_compliance_workspace`, registry entry 41 |
| api/main.py | include the compliance router |
| .github/workflows/ci.yml | compliance suites on SQLite and PostgreSQL |
| docs/msme/DOCUMENT_INTELLIGENCE.md | duplicates section rewritten |
| docs/msme/GAP_REGISTER.md | v5 |

## Behaviour changes to know about
- Documents whose duplicate flag changes (an older upload processed later) are now demoted from `approved` to `needs_review`.
- No new runtime dependencies (weasyprint was already a dependency; pdfplumber is used by tests only, via docket).
