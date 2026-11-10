# Credit readiness and scheme navigation (R-1, R-2)

Code: `msme/credit/` (facts, readiness, service, report, migration 42), `api/routers/msme_credit.py`, rules in
`msme/rules/` (see `RULE_SOURCES.md`). **This platform never contacts a lender, a scheme administrator or any government
system, never submits an application and never says anyone is "eligible" or "approved".**

## Facts (evidence model)
Only registered facts can be entered (`msme/credit/facts.py`): Udyam number/registration, sector, non-corporate/non-farm,
investment, turnover, credit amount, collateral-free request, prior Tarun loan repaid, loan purpose. Each value is validated,
stored append-only with `source` (`user_declared`), who entered it and when, and may link an uploaded document.
The newest row per key is current; older rows are history. `msme_category` is **derived** from investment and turnover by the
effective-dated rule engine and cannot be typed in; with partial evidence only a lower bound is shown.
The Udyam number's format is checked against the portal's own rule (`UDYAM-XX-00-0000000`); registration status is NOT verified
(the portal needs an OTP flow this platform does not integrate with).

## Readiness (R-1)
`GET /readiness` shows typical lender documents and what the platform can actually evidence, with no composite score:

| Item | How it is evidenced |
|---|---|
| Bank statements, 12 months | EVIDENCED/PARTIAL/MISSING from the date ranges of non-cancelled **imported** statements (union of ranges; exact gaps listed) |
| GST return history | USER_RECORDED from filings the user recorded in the compliance workspace (not verified against the portal) |
| Sales invoice evidence | months (12 complete) with at least one approved outward invoice |
| Udyam, financial statements, ITRs, KYC, purpose/quotation | user attestation with a note, optionally linking an uploaded document; always marked unverified |

Statuses: `EVIDENCED, PARTIAL, USER_RECORDED, USER_ATTESTED, MISSING, UNKNOWN, NOT_APPLICABLE`. The list of requirements is labelled
`UNVERIFIED_TYPICAL` (lender requirements vary; not an official list). Items the platform can evidence cannot be self-attested.

## Scheme assessment (R-2)
Each scheme's published criteria are compared with the current facts. Outcomes: `CRITERIA_NOT_MET`, `CRITERIA_APPEARS_MET`,
`INSUFFICIENT_EVIDENCE`; `lender_decision_required` is always true. Every criterion shows MET/NOT_MET/UNKNOWN, the basis, and the
**evidence source** (`user_declared`, `user_declared+document:<id>`, or `derived:msme-classification@<rule version>`).
Unknown criteria produce a plain-language **"what to provide next"** list (a missing category asks for investment and turnover).
Rule versions are effective-dated: on a date before a scheme's rules existed the result is `NOT_AVAILABLE_ON_DATE`, not a guess.
`POST /schemes/assess` stores an immutable snapshot per scheme (rule version, result, digest of the facts used) and writes one
hash-chained audit event. `GET /schemes/{id}/summary(.pdf)` is the exportable application-preparation summary: facts with sources,
criteria and evidence, gaps, readiness, typical documents (`UNVERIFIED_TYPICAL`), how to apply, official source with its
verification status, details deliberately not modelled, and a banner stating it is not an application or a decision.

## Declared-turnover cross-check (R-5)
`GET /turnover-check` compares the declared turnover with approved outward invoices and bank credits and returns HEURISTIC flags with
three standing caveats; see `RECONCILIATION.md`.

## API (/api/msme/credit)
| Method / path | Role |
|---|---|
| GET `/facts`, `/facts/history`, `/readiness`, `/turnover-check`, `/schemes`, `/schemes/assessments`, `/schemes/{id}/summary`, `/schemes/{id}/summary.pdf` | viewer |
| PUT `/facts`, `/readiness/attestations`; POST `/schemes/assess` | manager (not demo `public_manager`) |

## Verified (tests)
`tests/test_msme_credit.py` (63 checks) and `tests/test_msme_credit_api.py` (43 checks) on file SQLite and PostgreSQL 16 (backend
asserted): validation, append-only history, derived classification across rule dates, all assessment outcomes, snapshots and audit,
readiness evidence from real bank-statement rows (cancelled and other-company imports ignored, exact gap dates), real compliance tasks,
real approved documents, the PDF text and HTML escaping, concurrent writes with a valid audit chain. Six mutation checks (derived-only
category, newest-wins ordering, derived NOT_MSME, cancelled-import filter, lender-decision flag, report disclaimer) each fail a test.

## NOT verified / not built
- **Only two schemes** (PMMY, CGTMSE). No PMEGP, PM SVANidhi, CLCSS, state schemes, or lender products, so "scheme discovery" is narrow.
- Scheme rule data is secondary-sourced except the PMMY 2024 limit (PIB); CGTMSE coverage percentages, fees and above-INR-5-crore
  treatment are deliberately not modelled (sources conflict). Primary-source confirmation is outstanding.
- Turnover and investment are user-declared. A linked document is recorded but its contents are not checked against the value; no
  cross-check against approved-invoice totals yet.
- Bank coverage is by statement date range only (completeness of entries and balances are not assessed). GST history is user-recorded.
- Readiness is computed on demand and not persisted; only assessments are stored. No retention/deletion workflow for facts.
- No application submission, lender matching, or document packaging; no UI.
