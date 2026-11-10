# MSME Finance & Compliance Platform — start here

Lambda ERP (Apache-2.0, TORUS INVESTMENTS AG) extended for Indian MSMEs: document intelligence, a GST compliance workspace, credit-readiness and
government-scheme navigation, invoice-to-bank reconciliation, and a deployable Render Blueprint. Built in eight verified phases (`PHASES.md`).
**Preparation tooling only: it never files a return, contacts a government system, or says anyone is "eligible" or "approved".**

## Architecture (modular monolith)
```
 React/Vite UI (upstream; no MSME screens yet)            REST /api/*  (FastAPI, role-checked server-side)
        |                                                  |
        +-------------------------------------------------+
                                    |
   api/routers/msme_documents  msme_compliance  msme_credit  msme_reconciliation  ops          api/observability (request ids, JSON logs)
                                    |
   msme/documents      msme/compliance        msme/credit            msme/reconciliation     msme/rules      msme/security   msme/ops
   upload > OCR/LLM    deadlines, workpapers  facts, readiness,      explainable matching    effective-dated   upload          scheduled
   (docket-idp) >      maker-checker, reports scheme assessment      confirm/reject          MSME / GST /      validation      sweep
   cited fields,       hash-chained audit     turnover cross-check   DB-enforced 1:1         scheme rules
   corrections         (msme/compliance/audit)
                                    |
   lambda_erp (upstream ERP core, migrations 1-43)  ->  SQLite (dev) | PostgreSQL 16 (production)
   Document files: local/persistent disk (Render) or Azure Blob (optional)
```
Key properties: every write that matters is a hash-chained audit event in the same transaction; suggestions are heuristic until a person confirms; unknown evidence
is reported as unknown; rule data is effective-dated with its source and verification status; **one organisation per deployment** (`TENANCY.md`).

## Repository map (what is new)
| Path | Contents |
|---|---|
| `msme/` | the platform packages listed above (migrations 40-43 are registered in `lambda_erp/database.py`) |
| `api/routers/msme_*.py`, `ops.py`, `api/observability.py` | HTTP surface, readiness/status, structured logging |
| `render.yaml`, `Dockerfile`, `docker-entrypoint.sh` | Render Blueprint, image (tesseract, privilege-dropping entrypoint) |
| `tests/` | `test_msme_*`, `test_authz_matrix`, `test_sql_injection_probes`, `test_upload_security`, `test_render_blueprint`, `test_docker_entrypoint` ... |
| `docs/msme/` | this index, per-capability documents, `GAP_REGISTER.md`, `DEPLOYMENT_RENDER.md`, `history/` |
| `.github/workflows/` | `ci.yml` (all suites, SQLite and PostgreSQL), `security.yml` (pip-audit, npm audit, bandit, gitleaks) |

## Run it
```bash
pip install -e ".[postgres,msme,azure]" pytest pymupdf pyyaml      # plus the Tesseract binary (apt install tesseract-ocr)
export LAMBDA_ERP_AUTO_DEMO=0 JWT_SECRET_KEY=change-me DOCKET_TEXT_MODEL=<model> DOCKET_LLM_PROVIDER=<provider>
uvicorn api.main:app --port 8000
```
Tests are scripts (shared global DB state): `python -m tests.test_msme_compliance` etc.; PostgreSQL: set
`LAMBDA_ERP_TEST_DB=postgresql://user:pass@localhost:5432/db` (each test asserts the backend it ran on). `python -m pytest tests/test_msme_checks.py tests/test_msme_rules.py
tests/test_msme_matching.py ...` runs the pure unit tests. Storage tests need the Azurite emulator (`npm i -g azurite`); the entrypoint test needs root.

## Documents
`DEPLOYMENT_RENDER.md` (deploy, backups, limits) · `DOCUMENT_INTELLIGENCE.md` · `COMPLIANCE_WORKSPACE.md` · `CREDIT_AND_SCHEMES.md` · `RECONCILIATION.md` ·
`TENANCY.md` · `RULE_SOURCES.md` (what each rule was verified against) · `SECURITY_EXCEPTIONS.md` · `THIRD_PARTY.md` · `GAP_REGISTER.md` (**read this for what is and is not done**).

## Status in one paragraph
All 21 script suites and the pure unit tests pass on SQLite, and every database-backed suite passes on PostgreSQL 16 (see `GAP_REGISTER.md` for the measured
totals). **Not done:** actual cloud deployment (needs a Render account and GitHub repository), Render's own Blueprint validation, the Docker image build, GitHub Actions on a real
runner, a user interface for the MSME features, browser end-to-end tests, model-accuracy evaluation on real Indian invoices, primary-source confirmation of the rule data, any
government-portal or lender integration, and an automated off-platform backup of uploaded documents.
