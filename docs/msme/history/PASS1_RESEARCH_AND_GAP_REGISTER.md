# Pass 1 — Open-Source Evaluation (Phase One) + Gap Register v1 (Phase Two)

Assumption in force (no reply received on Pass 0 questions): Lambda ERP = foundation; scope = Problems 3.1 + 3.2.
All findings below come from cloned source inspected 2026-10-16 (shallow clones, depth 50).

## 1. Candidate comparison (evidence-based)

| Repo | License (file present?) | Stack | Last commit | Tests | Verdict |
|---|---|---|---|---|---|
| lambdadevelopment/lambda-erp | Apache-2.0 (yes) | FastAPI+React/TS, SQLite/Postgres | 2026-10-16 (v1.1.10) | ~46 files; validation suite green, 181 pytest pass | **FOUNDATION** |
| KazKozDev/docket | Apache-2.0 + NOTICE (yes) | Python lib + FastAPI, pydantic, Tesseract/pdfplumber | 2026-09-29 | 822 pass / 71 fail / 4 err (see below) | **ADOPT as dependency** for extraction layer |
| vas3k/TaxHacker | MIT (yes) | Next.js 16 / Prisma / LangChain | 2026-09-21 | 10 vitest files | Reference only (stack mismatch) |
| graceburns02/ai-document-intake | MIT (yes) | Streamlit, 521 LOC | 2026-05-26 | 3 files | Pattern reference only (too small) |
| angelstreet/openinvoice | MIT (yes) | Node/React, FR invoices (SIRET) | 2026-09-03 | none | Reject (no tests, FR-specific); cascade idea (regex→template→LLM) noted |
| stone16/Invoice-Manager | **No LICENSE file** (README badge only) | FastAPI+React, PaddleOCR | 2026-05-22 | 2 files | Reject: unclear license obligations; China fapiao domain |
| xieyunhu002/open-invoice | **No LICENSE file** (README text only) | Django+Vue, PaddleOCR | 2026-06-01 (1 commit) | none | Reject: no license file, no tests, China VAT-invoice domain |

### Docket test evidence (clean venv, Tesseract present)
822 passed, 10 skipped, 71 failed, 4 errors. 68 failures + 4 errors are `tests/test_einvoice.py` (needs downloaded
EN 16931/Peppol artefacts via `docket einvoice fetch` — EU e-invoicing, irrelevant to India; HTTP route returns 503 without them).
Remaining 3: OCR backend `auto` unavailable in test env config, env-var option test, eval-script import. None touch the
core extract/cite/validate path. Risk: einvoice subpackage must be excluded/unused; 64k LOC src means we pin a version and wrap it behind our own adapter.

### Why docket
Matches directive §6A directly: per-field source citations (traceability), deterministic checks (not "OCR = truth"),
review-queue reasons, corroboration, hostile-document security tests (`tests/security/`), eval harness (`eval/`) for AI regression sets,
provider abstraction (Ollama/OpenAI-compatible). Same license family as Lambda ERP (Apache-2.0). `pip check` clean when co-installed
with Lambda ERP deps.

### Decision record (hard-to-reverse flagged)
- D1 Foundation = Lambda ERP. Reversibility: low. Rationale: only candidate with double-entry GL, Postgres, roles, CI, Terraform, 181 passing tests.
- D2 Docket as pinned PyPI/Git dependency behind `msme/extraction/adapter.py`; **no vendoring**. Reversible (adapter boundary).
- D3 New code as modular monolith package `msme/` (+ routers under `api/routers/msme_*.py`), not edits to core `lambda_erp/` accounting.
- D4 India rules as **data** (versioned, effective-dated JSON/DB rows with source URL), not code constants. Hard to retrofit later.
- D5 No code copied from Invoice-Manager/open-invoice (license ambiguity).

## 2. Baseline security observations (from code read, to be verified by tests)
- Roles admin>manager>viewer (`api/auth.py`); uploads dir is per-user path, but DB-level tenant isolation between companies not yet proven — **S-1**.
- `api/attachments.py` stores on local disk (`UPLOAD_ROOT`), no object storage — **S-2/P-1**.
- CI has no dependency/secret scanning (no pip-audit/bandit/gitleaks) — **S-5**.

## 3. Gap Register v1 (all NOT STARTED unless stated)

Priority per directive §4 (1 = highest).

| ID | Pri | Requirement | Existing | Gap | Planned change | Acceptance test | Status |
|---|---|---|---|---|---|---|---|
| S-1 | 1 | Tenant isolation | user/role model, multi-Company doctype | No proven cross-tenant tests | Write isolation tests across list/get/attachment/report routes; fix leaks | Tenant A token gets 403/404 on all B resources | NOT STARTED |
| S-2 | 1 | Secure upload (type/size/magic bytes, malware-safe parsing) | `upload_attachment`, `_safe_ext` | Extension/mime trust; limits unverified | Magic-byte sniffing, size caps, hostile-PDF tests | Spoofed .pdf rejected; oversize 413 | NOT STARTED |
| S-3 | 1 | Prompt-injection defense for uploads | LLM chat reads attachments | Untrusted text reaches tool-calling agent | Docs-as-data wrapper; tools stay on `tool_permissions` | Injection corpus cannot trigger writes | NOT STARTED |
| S-5 | 1 | Dep/secret scanning in CI | none | missing | pip-audit, npm audit, gitleaks in `ci.yml` | CI gate fails on seeded finding | NOT STARTED |
| D-1 | 2 | Document intelligence w/ confidence + source citation | OpenAI multimodal only | No OCR adapter, no field confidence/trace | Docket adapter → `Extracted Field` records (value, confidence, page/bbox, source text) | Fixture invoice yields cited fields | NOT STARTED |
| D-2 | 2 | Human correction + reprocess, immutable original | none | missing | Correction history table; originals preserved | Edit keeps original + audit row | NOT STARTED |
| D-3 | 2 | Duplicate + idempotent processing | none verified | missing | SHA-256 + (supplier, number, date, total) key | Re-upload returns same job | NOT STARTED |
| F-1 | 2 | Reconciliation: duplicates, arithmetic, 3-way mismatch, unmatched | bank reconciliation exists (CAMT) | Invoice-level validation + confirmed vs heuristic classes | `msme/reconcile` rules engine with severity `CONFIRMED`/`HEURISTIC` | Seeded defects detected, clean set yields none | NOT STARTED |
| C-1 | 2 | GST/MSME compliance calendar, effective-dated rules | none | missing | Rule tables with `effective_from/to`, `source_url`, `version`; deadline generator | Due dates match cited official calendar for fixtures | NOT STARTED |
| C-2 | 2 | Workpapers, approval workflow, audit trail, reports | Lambda has doc workflow/versioning | Maker-checker + export | Approval policy + PDF/XLSX export (weasyprint/openpyxl already deps) | Draft→approved flow; draft never labeled "filed" | NOT STARTED |
| R-1 | 2 | Credit-readiness assessment | none | missing | Document-readiness checklist engine | Missing docs listed with reasons | NOT STARTED |
| R-2 | 2 | Scheme discovery + transparent eligibility | none | missing; needs authoritative sources | Scheme catalogue (data, official URLs, as-of dates), rule evaluator returning MET/NOT_MET/UNKNOWN + evidence | Unknown evidence → UNKNOWN, never "eligible" | NOT STARTED (needs source verification pass) |
| P-1 | 3 | Private persistent object storage | local disk | Container FS ephemeral | Storage interface; Azure Blob impl (+ local for dev) | Upload survives restart | NOT STARTED |
| T-1 | 4 | Fix failing upstream test | Swiss pack test (:208) 6 vs 7 | drift | Diagnose; fix test or code | Test green | NOT STARTED |
| T-2 | 4 | Browser E2E | none | missing | Playwright journeys | Upload→review→approve passes | NOT STARTED |
| X-1 | 5 | Cloud deployment | Terraform for Azure Container Apps, deploy.yml | No credentials/authorization here | Prepare; document unblock steps | Live URL verified | **BLOCKED** (external) |
| O-1 | 6 | Health/readiness, structured logs, correlation IDs | Azure Monitor doc exists | to verify | Audit + add | `/healthz`,`/readyz` tests | NOT STARTED |

## 4. Third-party attribution record (v1)
| Component | Use | License | Notice obligation |
|---|---|---|---|
| lambda-erp (TORUS INVESTMENTS AG) | foundation | Apache-2.0 | keep LICENSE; mark modified files |
| docket-idp (A. Kazakov Kozlov) | dependency | Apache-2.0 (+ NOTICE: KoSIT Apache-2.0, ConnectingEurope EUPL-1.2, OASIS UBL) | reproduce NOTICE; do not ship einvoice artefacts unless needed |
| TaxHacker, ai-document-intake, openinvoice | ideas only, no code | MIT | none while no code copied |
| Invoice-Manager, open-invoice | not used | unclear | n/a |

## 5. Next pass (Pass 2)
1. Web-verify authoritative India sources (GST portal calendar, Udyam/MSME ministry, CGTMSE, Mudra/PMEGP/PM-SVANidhi etc.) and build effective-dated rule data.
2. Implement S-1/S-2 tests first (security before features), then D-1 adapter with fixtures.
3. Deliver as zip with NEW vs MODIFIED file list.
