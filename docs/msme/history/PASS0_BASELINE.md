# Pass 0 — Baseline Report (Phase Zero)

## Scope finding (blocking ambiguity)
The uploaded zip `lambda-erp-master` is **upstream Lambda ERP v1.1.10** (Apache-2.0, TORUS INVESTMENTS AG),
i.e. candidate #1 from Phase One — not a HackCulture assessment project.
- No `.git` directory (no history/branches/uncommitted work to preserve).
- Zero MSME-specific code: no GST/Udyam/MSME/scheme/credit-readiness modules (grep evidence: only incidental
  hits in `profiles.py`, `generic.py`, `chat.py`, `company_setup.md`, `release.yml`).
- No assessment criteria document present; problem 3.1 vs 3.2 applicability cannot be determined from the repo.

## Verified (evidence: commands run in sandbox)
| Check | Result |
|---|---|
| `python -m tests.test_erp_validation` | exit 0, trial balance balanced (57,273.50 Dr = Cr) |
| `pytest tests` (excl. validation) | 181 passed, 1 skipped, 14 failed |
| 13 of 14 failures | Test-isolation artifact: `test_company_setup.py` is designed for `python -m`, shares global in-memory DB under pytest; 15/16 pass in isolation |
| 1 genuine failure | `test_swiss_pack_applies_with_resolvable_defaults_and_tax` (`:208`): expects 6 `tax_summary` entries, gets 7 (extra "default sales tax" line) — upstream test/code drift, fails under `python -m tests.test_company_setup` too |
| Frontend `tsc --noEmit` | exit 0 |
| Frontend `npm run build` | success; >500 kB chunk warning |

## What exists (relevant to MSME outcomes)
- Working: double-entry accounting, sales/purchase cycle, stock, bank statement import (CAMT) + reconciliation,
  RBAC-ish roles (`api/auth.py`, `tool_permissions.py`), LLM chat agent (OpenAI), attachment upload
  (`api/attachments.py`, per-user upload dir), PDF output, SQLite + Postgres, Docker, Terraform (Azure Container Apps), CI.
- Partial: document intelligence — attachments go to OpenAI multimodal; no OCR adapter, no field-level confidence,
  no source-region traceability, no duplicate-hash detection verified yet.
- Missing: GST/compliance calendar + effective-dated rules, workpapers/approval workflow, credit-readiness,
  scheme catalogue/eligibility engine, tenant isolation (single-company-per-DB model to be verified), object storage
  (local disk uploads).
- Not verifiable here: Azure deployment, OpenAI behavior (no key), Postgres CI job, browser E2E.

## Blocked by external access
Cloud deploy (no credentials/authorization), LLM API key, official scheme source verification (needs search/fetch pass).

## Needed to proceed (minimum)
1. Confirm: is this zip the intended foundation, or is a separate HackCulture project/assessment brief coming?
2. Which problem applies: 3.1, 3.2, or both?
Default if no answer: Lambda ERP as foundation, both problems, in-repo modular additions.
